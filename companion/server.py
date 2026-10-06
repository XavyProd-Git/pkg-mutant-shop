#!/usr/bin/env python3
"""
PKG MUTANT SHOP - companion server
============================================
Stdlib only (Python 3.8+; Pillow for card thumbnails). No pip installs, no CDNs, no external services.
The PC half of the app: serves the library over HTTP Range, the on-console ELF installs from it.

v0.4 adds: multi-console fleet + "send to all", parallel queue with per-console serialization,
unified REAL progress (local byte-count OR polling a remote companion's /api/served), SHA-256
integrity, per-title manifests.

Honest markers:  # REAL works now   # ON-DEVICE real code, console confirms it
"""

import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import sqlite3
import tempfile
import uuid
import platform
import gzip
import threading
import time
import traceback
import ftplib
import shutil
import urllib.request
import urllib.error
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, unquote, quote, parse_qs

import pkg_meta
import payloads as payload_engine
import sources as source_engine

VERSION = "3.96.0"

# The newest PS5 firmware this build can run on. It is the ceiling of the kernel-offset table in the
# payload SDK the ELF was compiled against - past it the ELF cannot establish kernel read/write and
# dies before main() reaches its first log line, with no process, no log and no message. Declared
# here because the README, the release notes and the firmware badge all have to say the same number
# and nothing was keeping them honest: the front page still said 12.70 three firmwares later.
# `bash ps5-app/update-sdk-wsl.sh --check` prints what the installed toolchain actually knows.
PS5_FW_SUPPORTED = "13.60"

_BUILD_ID = None


def build_id():
    """A short fingerprint of THIS artifact, beside the version.

    A version number does not change when the code does, and two machines running "3.64.0" have
    already turned out to be running different builds - a copy that silently failed, and an exe
    rebuilt after a fix. There was no way to see that from either screen. This is what is actually
    running: the exe's own bytes when frozen, the server plus the page when running from source.

    Cheap and lazy: computed on the first call and kept. Hashing a 22 MB exe costs about a tenth of
    a second, once, and only if something asks."""
    global _BUILD_ID
    if _BUILD_ID is not None:
        return _BUILD_ID
    h = hashlib.sha256()
    try:
        if getattr(sys, "frozen", False):
            with open(sys.executable, "rb") as f:
                for chunk in iter(lambda: f.read(1 << 20), b""):
                    h.update(chunk)
        else:
            for p in (os.path.abspath(__file__),
                      os.path.join(WEB_DIR, "index.html")):
                try:
                    with open(p, "rb") as f:
                        h.update(f.read())
                except OSError:
                    pass
    except Exception:
        _BUILD_ID = ""
        return _BUILD_ID
    _BUILD_ID = h.hexdigest()[:8]
    return _BUILD_ID

# HOW LONG A PROBE ANSWER ON THE /api/health PATH STAYS GOOD.
#
# Four memos on that path were each written as 5.0 s "because health is polled every 6 s". Read
# that again: the answer is thrown away ONE SECOND BEFORE the poll that would have used it. For a
# single viewer - the ordinary case, one browser open - the hit rate was exactly zero. Every memo
# did nothing but hold data long enough to be stale, while every poll still paid four probes into
# a console's single-threaded accept loop.
#
# The number has to be LONGER than the interval it is protecting, not shorter. At 11 s a viewer
# polling every 6 s probes on one poll and reads the memo on the next, so the console sees half as
# many probes and nothing on screen is ever more than about two polls old. The two numbers are
# defined next to each other here so the relationship between them is visible, because it was the
# relationship - not either value - that was wrong.
HEALTH_POLL_S = 6.0      # web/index.html: setInterval(refreshHealth, 6000)
HEALTH_MEMO_S = 11.0     # deliberately > HEALTH_POLL_S; see above

if getattr(sys, "frozen", False):          # PyInstaller one-file .exe
    HERE = os.path.dirname(sys.executable)
    WEB_DIR = os.path.join(sys._MEIPASS, "web")
else:
    HERE = os.path.dirname(os.path.abspath(__file__))
    WEB_DIR = os.path.normpath(os.path.join(HERE, "..", "web"))
# The cheat + patch library. WHERE IT ACTUALLY LIVES - and this comment has been wrong before, so it
# is written from the files rather than from memory:
#
#   the PS5 ELF embeds all of it (cheat_bundle.h + cheats.pack, 7022 files) and writes it to that
#   console itself at boot, so a PS5 never needs the PC for cheats;
#
#   THE EXE DOES CARRY IT. PKG-MUTANT-SHOP.spec has four datas entries, one of them
#   ('../assets/cheats-library.zip', 'cheats-pack'), and _unpack_cheat_library() below expands that
#   archive into HERE/cheats on a daemon thread at startup - which IS CHEATS_DIR when frozen. The
#   previous version of this comment said the exe bundled "ONLY web/" and that the sync therefore had
#   "nothing to push - by design". Both halves were false, and the PS4 depends on this: it cannot
#   embed the library, so it takes it from the PC;
#
#   from source, assets/cheats is the repo copy, and the sync is also a repair path for a console
#   whose /data was wiped.
# THE PS4 DASHBOARD APP. A real PS4 application package (PKGM00001) that puts the shop on the
# console's home screen; built by ps4-app/tile-pkg/build-wsl.sh and shipped inside the exe, because
# a PS4 whose only copy of the shop is a payload has nothing to press once a rest cycle takes the
# jailbreak's loader away. Installed through the ordinary install lane - it IS an ordinary package.
if getattr(sys, "frozen", False):
    PS4_TILE_PKG = os.path.join(sys._MEIPASS, "ps4-tile",
                                "IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg")
else:
    PS4_TILE_PKG = os.path.normpath(os.path.join(
        HERE, "..", "ps4-app", "tile-pkg", "IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg"))
# THE PS4 PAYLOAD, so this PC can start a PS4's shop without anyone pressing the icon. Bundled
# into the exe beside the tile package: ~9 MB, and it is the difference between "switch the PS4 on"
# and "switch the PS4 on, then go and find the icon". The PS5's payload is deliberately NOT bundled
# - it is 34 MB, and its own lane (Payload Manager) is a different mechanism with its own tool.
if getattr(sys, "frozen", False):
    PS4_PAYLOAD_ELF = os.path.join(sys._MEIPASS, "ps4-elf", "PKG-MUTANT-SHOP-PS4.elf")
else:
    PS4_PAYLOAD_ELF = os.path.normpath(os.path.join(
        HERE, "..", "ps4-app", "onconsole", "PKG-MUTANT-SHOP-PS4.elf"))

PS4_TILE_KEY = "PKG-MUTANT-SHOP-PS4-APP.pkg"
PS4_TILE_TID = "PKGM00001"
PS4_TILE_CID = "IV0000-PKGM00001_00-PKGMUTANTSHOP001"

# WHERE THE LIBRARY IS, AND HOW IT GETS THERE IN A FROZEN BUILD.
#
# From source it is the repo's own folder. Frozen, it is a folder beside the exe that is expanded
# once out of the archive the exe carries (see _unpack_cheat_library below) - NOT sys._MEIPASS,
# which this line used to point at. _MEIPASS is the one-file build's scratch directory: PyInstaller
# unpacks every bundled data file into it on EVERY launch and deletes it on exit, so putting six
# thousand cheat files there would mean six thousand file writes each time the app starts. Beside
# the exe, they are written once and stay.
if getattr(sys, "frozen", False):
    CHEATS_DIR = os.path.join(HERE, "cheats")
    CHEATS_ZIP = os.path.join(sys._MEIPASS, "cheats-pack", "cheats-library.zip")
else:
    CHEATS_DIR = os.path.normpath(os.path.join(HERE, "..", "assets", "cheats"))
    CHEATS_ZIP = ""


def _unpack_cheat_library():
    """Expand the bundled cheat library beside the exe, once, and say nothing if there is none.

    A PS5 carries its own copy inside its ELF. A PS4 cannot (its payload lives in a shared system
    daemon), so it takes the library from this PC - and before this, a PC running the shipped exe
    had no library to give. The stamp file names the archive that produced the folder, so a release
    with a different library replaces it and every other run costs one read of 12 characters."""
    if not CHEATS_ZIP or not os.path.exists(CHEATS_ZIP):
        return
    import hashlib
    import zipfile
    try:
        ident = hashlib.sha256()
        with io.open(CHEATS_ZIP, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                ident.update(chunk)
        ident = ident.hexdigest()[:12]
        stamp = os.path.join(CHEATS_DIR, ".library-id")
        try:
            if io.open(stamp, encoding="utf-8").read().strip() == ident:
                return
        except Exception:
            pass
        os.makedirs(CHEATS_DIR, exist_ok=True)
        with zipfile.ZipFile(CHEATS_ZIP) as z:
            names = z.namelist()
            for name in names:
                # Paths come from an archive this build made, but an archive is still input: a
                # name that climbs out of the folder is refused rather than trusted.
                if name.endswith("/") or ".." in name.replace("\\", "/").split("/"):
                    continue
                dest = os.path.join(CHEATS_DIR, name.replace("/", os.sep))
                if not os.path.abspath(dest).startswith(os.path.abspath(CHEATS_DIR) + os.sep):
                    continue
                if os.path.exists(dest):
                    continue                      # never overwrite a file the owner put here
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                # A DOTFILE TEMP, in the same folder as its target. These four folders are exactly
                # what the console sync pushes FROM, and the listing there excludes dotfiles - so
                # naming the temp this way means a sync running while the library is still expanding
                # cannot pick up a half-written file. It was "<name>.part", which was a legitimate
                # member of that listing.
                tmp = os.path.join(os.path.dirname(dest), "." + os.path.basename(dest) + ".part")
                with z.open(name) as src, io.open(tmp, "wb") as out:
                    out.write(src.read())
                os.replace(tmp, dest)             # nothing half-written is ever left behind
        io.open(stamp, "w", encoding="utf-8").write(ident)
        print("[cheats] library ready: %d file(s) in %s" % (len(names), CHEATS_DIR))
    except Exception as e:
        # A PC that cannot write beside its own exe has no library to push, which is exactly where
        # this started and is better than an app that will not start.
        print("[cheats] could not expand the bundled library: %s" % e)
# Only what the on-console engine reads (server.c: CHEAT_JSON_DIR / SHN / MC4 / PATCH_DIR). The
# xml/xml_orbis/xml_prospero folders are the pre-migration layout the console never opens, so
# pushing them cost transfer for files nothing looks at.
CHEAT_SUBDIRS = ("json", "mc4", "shn", "patches")
CONSOLE_CHEAT_ROOT = "/data/pkg-mutant-shop/cheats"


def _cheat_local_files(sub):
    """Names of the shipped cheat files in one library subfolder, as a set.

    A `<name>.mc4.xml` is the decrypted twin of a .mc4 the console decrypts itself (706 of them
    sit beside the .mc4 files); the engine never opens one, so sending them was pure waste."""
    ldir = os.path.join(CHEATS_DIR, sub)
    if not os.path.isdir(ldir):
        return set()
    # .part IS EXCLUDED TOO, belt and braces. The unpack now writes its temps as dotfiles, which the
    # test above already covers, but this listing is the ONE place these four folders are enumerated
    # for the push - so the rule belongs here as well, where it cannot be bypassed by some future
    # writer that chooses a different temp name.
    return {n for n in os.listdir(ldir)
            if not n.lower().endswith(".mc4.xml") and not n.lower().endswith(".part")
            and not n.startswith(".")}

# ---------------------------------------------------------------------------------------------
# [audit 17] A LOG, because the shipped exe had none.
#
# Every diagnostic in this file is a print(), and the exe is built --noconsole: stdout is a null
# device. So when an install fails with "the console rejected this PKG", the daemon's real reply -
# the SCE error code, which FTP port answered, whether a reload was even attempted - was printed
# and discarded. The user was left with a one-line UI message and no way to find out anything else.
#
# print() is wrapped rather than replaced at every call site: ~200 existing prints become useful
# immediately, and stdout still works when running from source.
# ---------------------------------------------------------------------------------------------
LOG_PATH = os.path.join(HERE, "pms.log")
LOG_MAX_BYTES = 2 * 1024 * 1024
_log_lock = threading.Lock()
_log_fh = None


def _log_open():
    """Open the log, rotating one generation if it has grown past the cap. Never raises: a logging
    problem must not be able to stop the app it is logging for."""
    global _log_fh
    try:
        if os.path.exists(LOG_PATH) and os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
            old = LOG_PATH + ".1"
            try:
                if os.path.exists(old):
                    os.remove(old)
                os.replace(LOG_PATH, old)
            except OSError:
                pass
        _log_fh = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
        _log_fh.write("\n==== PKG MUTANT SHOP %s started %s ====\n"
                      % (VERSION, time.strftime("%Y-%m-%d %H:%M:%S")))
        _log_fh.flush()
    except Exception:
        _log_fh = None


_real_print = print
_log_lines = 0                                     # lines since the size was last checked


def _log_rotate_locked():
    """Roll pms.log over to pms.log.1 and reopen. Caller holds _log_lock. Never raises.

    _log_open() rotated once, at startup, so a PC that runs for weeks with a chatty payload on
    the :9097 relay grew the file past LOG_MAX_BYTES with nothing to stop it. Same one-generation
    scheme as startup, applied while running."""
    global _log_fh
    try:
        _log_fh.close()
    except Exception:
        pass
    _log_fh = None
    try:
        old = LOG_PATH + ".1"
        if os.path.exists(old):
            os.remove(old)
        os.replace(LOG_PATH, old)
    except OSError:
        pass
    try:
        _log_fh = open(LOG_PATH, "a", encoding="utf-8", errors="replace")
        _log_fh.write("==== PKG MUTANT SHOP %s log rotated %s ====\n"
                      % (VERSION, time.strftime("%Y-%m-%d %H:%M:%S")))
        _log_fh.flush()
    except Exception:
        _log_fh = None


def print(*args, **kwargs):                       # noqa: A001 - deliberate shadow
    """print() that also lands in pms.log, with a timestamp."""
    global _log_lines
    try:
        _real_print(*args, **kwargs)
    except Exception:
        pass                                       # no console attached (frozen, --noconsole)
    if _log_fh is None:
        return
    try:
        line = kwargs.get("sep", " ").join(str(a) for a in args)
        with _log_lock:
            _log_fh.write("%s  %s\n" % (time.strftime("%H:%M:%S"), line))
            _log_fh.flush()                        # a crash must not swallow the last lines
            # Size check every ~200 lines, not every line: a stat per print is not free and the
            # cap only needs to hold roughly, not to the byte.
            _log_lines += 1
            if _log_lines >= 200:
                _log_lines = 0
                try:
                    if os.path.getsize(LOG_PATH) > LOG_MAX_BYTES:
                        _log_rotate_locked()
                except OSError:
                    pass
    except Exception:
        pass


CACHE_DIR = os.path.join(HERE, ".cache")
ICON_DIR = os.path.join(CACHE_DIR, "icons")
# Card-sized covers. The originals are 512x512 PNGs averaging ~280 KB, which every browser has
# to decode to a full 1.0 MB of RGBA - for a tile drawn about 220 px wide. On the PS5 that is
# the whole performance story: thirty visible cards meant thirty megabytes of decoded bitmap.
# A 320 px WebP costs roughly 15-25 KB and a quarter of the decode, and is indistinguishable at
# card size. Originals are kept untouched for the detail panel.
THUMB_DIR = os.path.join(CACHE_DIR, "thumbs")
HASH_CACHE_PATH = os.path.join(CACHE_DIR, "hashes.json")
# CONFIG_PATH: beside this file, or beside the exe when frozen - and overridable by PMS_CONFIG.
#
# The override exists because the test suites had to WRITE OVER the repo's own config.json to run,
# then put it back afterwards. That works until one of them is interrupted, and then the owner's
# settings are whatever the test left behind. A second instance for a UI check had the same
# problem: there was no way to start one on another port without editing the file the real one
# reads. An environment variable costs nothing and removes both.
CONFIG_PATH = os.environ.get("PMS_CONFIG") or os.path.join(HERE, "config.json")
EXAMPLE_PATH = os.path.join(HERE, "config.example.json")
INSTALLED_PATH = os.path.join(HERE, "installed.json")

# The one true library root. Created automatically if missing so a fresh install just works.
LIBRARY_ROOT = r"C:\Mutant Games"
LIBRARY_LAYOUT = ("PS4", "PS5")

DEFAULT_CONFIG = {
    # EMPTY, NOT AN EXAMPLE ADDRESS. This used to ship as "192.168.1.50", and reconcile_consoles
    # then treated any address equal to it as "not configured" - so a person whose PS5 really is at
    # 192.168.1.50 had their PS5 entry deleted from the fleet every time the config was reconciled,
    # and on a machine with a PS4 as well they were left with the PS4 and no PS5 at all. An empty
    # default says the same thing ("I have not been told") without ever colliding with a real LAN.
    "ps5_ip": "",
    # The PS4's address, empty until someone has one. `consoles` is the list everything actually
    # runs on; these two keys are the two boxes the settings panel offers, reconciled into it by
    # reconcile_consoles(). Empty means "I do not have that console", not "look for it".
    "ps4_ip": "",
    # Bumped only by a migration that has to run once. See load_config.
    "config_rev": 1,
    "consoles": [],
    # OUR engine is the only install lane: the companion serves the pkg, the on-console ELF
    # streams it to console storage and registers it itself. Nothing third-party is involved, and
    # it never enters the console's BGFT download queue, so a stalled system job cannot wedge it.
    # The section keeps its old "dpi" name only so an existing config.json still parses.
    # `pldmgr_port` is the one key still read - it is how the
    # console spawns our installer. The rest (mode, host, port_v2, port_v1, port_ezremote,
    # auto_reload, live_probe) described a third-party install daemon and are gone; anything left
    # in an existing config.json is simply ignored.
    "dpi": {"pldmgr_port": 8084},
    "ftp": {"port": 2121},
    # notify_port is NOT read any more (nothing has ever listened on 9099 - toasts go through the
    # ELF's /api/notify). It stays in the defaults only so an existing config.json keeps its shape.
    "console": {"app_db_path": "/system_data/priv/mms/app.db", "notify_port": 9099},
    # PS5 backups land in /mnt/<drive>/homebrew; ShadowMount scans and mounts them itself.
    # port 10101, NOT 9021: ShadowMountPlus announces its own listener in its log
    # ("[API] HTTP/JSON ready: http://127.0.0.1:10101/api/v1"). 9021 is elfldr, the ELF loader,
    # which is always running - so probing it reported ShadowMount "active" no matter what.
    # It binds loopback-only, so only the console itself can answer this; we just report it.
    "shadowmount": {"port": 10101, "scan_path": "/data/homebrew", "scan_paths_by_drive": {}},
    "cheats": {"root": "/data/cheatrunner"},
    "install": {"ftp_dest": "/data/pkg"},
    "library": {"local_paths": [LIBRARY_ROOT], "sources_file": "sources.json", "serve_local_over_http": True},
    "sources": [],
    "queue": {"max_parallel": 2, "allow_parallel_per_console": False},
    "companion": {"host": "0.0.0.0", "port": 8710},
    # The DNS answer for the consoles. A jailbroken PS4 that can reach PlayStation Network refuses
    # to install anything the Store has an update for, and deletes fake-signed retail titles on
    # restart - both measured. See DnsBlocker. Point the console's DNS at this PC to use it.
    "psn_block": {"enabled": True, "listen_ip": "", "upstream": "1.1.1.1", "extra_blocked": []},
    "drives": ["internal", "ext0", "ext1", "usb0", "usb1", "usb2", "usb3", "usb4", "usb5", "usb6", "usb7"],
    "maintenance": {"auto_rescan": True, "rescan_interval_sec": 10, "rescan_settle_sec": 3,
                    "temp_sweep_age_sec": 3600},
    "performance": {"serve_chunk_bytes": 1048576},
    "integrity": {"enabled": True, "verify_before_install": True, "on_local_corrupt": "warn"},
    "federation": {"enabled": False, "name": "", "peers": []},
    # OUR OWN RELEASES. `channel` decides which of our releases this device is offered: "stable" is
    # a published GitHub release, and only the big versions are published - see internal/RELEASING.md,
    # is the whole reason this is a channel and not "is there a newer number anywhere".
    # `github_token` is needed ONLY while our repository is private; every third-party upstream in
    # curated.json is public and reads fine without it. It is never logged and goes nowhere except
    # api.github.com.
    # auto_restart: restart the moment the new build is in place, without asking. OFF by default
    # now: the app fetches and installs its own update on its own, and then says "ready - restart
    # when you like", the way every other desktop app does. Closing the window someone is using,
    # mid-install-queue, because a release happened is not a thing to do unasked. The update is on
    # disk either way; this only decides who picks the moment.
    # self_update: the background check that keeps THIS app current. Off makes the app behave as it
    # did before - the panel still offers the update, it just will not go and get it by itself.
    "updates": {"channel": "stable", "github_token": "", "check_on_start": True,
                "auto_restart": False, "self_update": True, "check_every_hours": 6},
    "content_ownership_ack": False,
}


def install_error_text(rc):
    """Turn an SCE install error code into a sentence. The SAME TABLE OF CODES as install_error_text()
    in ps5-app/onconsole/server.c and installer_probe.c - add a code to one, add it to all three.
    The wording is kept in step by hand and already differs slightly between the copies (the
    console says "the PC sharing it", this side says "this PC"), so the old "identical" claim
    here was not true; what must agree is which codes are recognised, not the exact sentence.

    `rc` may be an int or a "0x80B2116F" string; anything unparseable still produces a true
    sentence, which is what stops this table from needing to be exhaustive.
    """
    try:
        code = int(str(rc), 16) if isinstance(rc, str) else int(rc or 0)
    except (TypeError, ValueError):
        code = 0
    if code == 0x80B2116F:
        return ("The console turned the request down before it started - reload PKG MUTANT SHOP "
                "on the PS5 from Payload Manager and try again")
    if code == 0x80B22404:
        return "The console could not fetch the file - check this PC is still sharing it"
    if code == 0x80B21104:
        # No bgft row is created at all for this one. Measured with 20.1 GB free and an 85.29 GB
        # package whose own integrity check passed. It used to fall into the generic 0x80B2xxxx
        # bucket below, which tells the user to download the package again - on an 85 GB file, the
        # one action that cannot possibly help.
        return ("The console would not start this install - it is usually not enough free space "
                "on the drive it installs to")
    if code == 0x80F00003:
        return ("The console does not recognise this file as a package - it may be a backup, "
                "not a PKG")
    if (code & 0xFFFF0000) == 0x80A40000:
        return ("The console's own game database refused it - rebuild the database from Safe Mode, "
                "then try again")
    if (code & 0xFFFF0000) == 0x80B20000:
        return ("The console's installer refused it - the package may be incomplete, so download "
                "it again")
    return "The console refused it - nothing was installed"


class ShopHTTPError(Exception):
    """The console answered, with an HTTP error status. It is REACHABLE - do not tell the user to
    reload it. `payload` is whatever JSON it sent, which normally carries its own sentence."""

    def __init__(self, status, payload, body, url):
        self.status = status
        self.payload = payload or {}
        self.body = body
        self.url = url
        Exception.__init__(self, "%s -> HTTP %d: %s" % (url, status, (body or "")[:180]))

    def sentence(self, fallback):
        """What the console itself said, if it said anything useful."""
        return self.payload.get("message") or self.payload.get("error") or fallback


# engine_words() and drive_label() lived here. Neither had a caller anywhere (server.py, index.html,
# the tools), and drive_label's "Internal M.2"/"EXT1" disagreed with what the app actually shows
# (the console's own /api/devices labels). Dead words that disagree are worse than none.


def viewer_platform_for(consoles, peer):
    """Which console is READING the page - "ps4", "ps5", or "" for a PC.

    The shop's UI is one file, served by the PC and by both consoles, but the data always comes
    from the PC. So the page cannot tell on its own what it is being read on, and it has to know:
    a PS4 must not be offered PS5 games, the header has to name the right console, and the row
    about mounted game backups is a PS5 row that has no meaning on a PS4.

    The answer is the requesting address, which is free and exact - the console asking is the
    console being read on. A console that names no platform is a PS5, the rule this file follows
    everywhere, because a config written before PS4 support existed carries no platform at all.

    Split out of the health handler so it can be tested without two consoles and a television.
    """
    if not peer:
        return ""
    for c in (consoles or []):
        if str((c or {}).get("ip") or "") == str(peer):
            return str(c.get("platform") or "").lower() or "ps5"
    return ""


def _is_mount_request(body, srv):
    """Is this install a BACKUP CONTAINER - the play-in-place lane - rather than a package?

    Same question the UI asks with installsAsBackup(): the item's own kind when the library states
    one, otherwise its extension. Answered here as well as there because a refusal the server does
    not make is a refusal a second client does not get, and this one guards a multi-gigabyte
    transfer to a console that cannot use it."""
    if not isinstance(body, dict):
        return False
    if str(body.get("kind") or "").lower() == "backup":
        return True
    if str(body.get("lane") or "").lower() in ("mount", "peer-mount"):
        return True
    key = str(body.get("install_key") or body.get("url") or "")
    name = key.rsplit("/", 1)[-1].split("?", 1)[0].lower()
    return name.endswith(MOUNT_EXTS) if name else False


def _bridge_for(srv, handler=None, body=None, want=None):
    """The console a request actually means.

    _first_bridge() answers "consoles[0]", which is the PS5 on a machine that has both. That was the
    right answer while there could only be one console and is the wrong one now: a page open on the
    PS4 asking about storage got the PS5's drives, and - the dangerous one - /api/rest/prepare could
    stop the payloads on a console that has none while telling the owner it was safe to rest the one
    that does. A PS5 woken from rest with its payloads still loaded panics.

    In order, stopping at the first that answers:

      1. THE CALLER NAMED ONE - `console` in the body, or ?console= in the query. An explicit name
         wins even when it is the "wrong" platform for the route: if someone points a PS5-only
         action at a PS4, the PS4's own refusal is the honest reply, and quietly doing it to the
         other console instead is how you act on a machine nobody asked you to touch.
      2. THE REQUEST CAME FROM A CONSOLE. The page is served BY the console it is read on, so a
         request from that console's address is about that console. Free and exact.
      3. THE ROUTE REQUIRES A PLATFORM (`want`). Reserved for things only one kind of console has -
         stopping payloads before rest, the payload autostart, the install-host reload. If no
         console of that kind is configured the answer is None, and the caller says so.
      4. The first console, which is what every one of these routes did before.

    `console=all` is not a console: it is what the picker sends for "every console", and the reads
    that use this helper want one. It falls through to the ordinary order rather than failing.
    """
    fleet = getattr(srv, "fleet", None)
    ids = fleet.ids() if fleet else []
    if not ids:
        return None

    named = ""
    if isinstance(body, dict):
        named = str(body.get("console") or "").strip()
    if not named and handler is not None:
        try:
            named = (parse_qs(urlparse(handler.path).query).get("console") or [""])[0].strip()
        except Exception:
            named = ""
    if named and named.lower() != "all":
        b = fleet.bridge(named)
        if b is not None:
            return b

    if handler is not None:
        try:
            peer = (handler.client_address or ("",))[0] or ""
        except Exception:
            peer = ""
        if peer:
            for cid in ids:
                b = fleet.bridge(cid)
                if b is not None and str(getattr(b, "ip", "")) == peer:
                    return b

    if want:
        for cid in ids:
            b = fleet.bridge(cid)
            try:
                plat = b.platform_id() if b is not None else None
            except Exception:
                plat = None
            if b is not None and plat == want:
                return b
        return None

    return fleet.bridge(ids[0])


def _first_bridge(srv):
    return srv.fleet.bridge(srv.fleet.ids()[0]) if srv.fleet.consoles else None


def _ps4_bridge(srv):
    """The console that IS a PS4 - as the console itself reports it, not as config.json spells it.

    /api/ps4/tile used to pick its console by reading the `platform` field of each config entry. A
    hand-written entry has no such field (internal/SETUP-REMOTE.md documents exactly that shape) and neither
    does one written before PS4 support, so the Settings panel hid the PS4 dashboard-app card
    entirely - console:false - while the PS4 was online, listed and installing games perfectly.
    Bridge.is_ps4() answers from /api/health and remembers it, so a configured platform still wins
    and an unstated one is learned instead of guessed.

    Deliberately NOT _bridge_for(want="ps4"): that helper answers "the request came from a console"
    before it ever looks at the platform, and the page open on the PS5 reaches this PC from the
    PS5's own address - so it would hand back the PS5 for a route only a PS4 can serve."""
    fleet = getattr(srv, "fleet", None)
    for cid in (fleet.ids() if fleet else []):
        b = fleet.bridge(cid)
        try:
            if b is not None and b.is_ps4():
                return b
        except Exception:
            continue
    return None


_TID_NAME_RE = re.compile(r"^(?:CUSA|PPSA|PLAS|NPXS|NPUB|NPEB)\d{4,5}$", re.I)


def is_placeholder_name(name, tid=None):
    """True when `name` is really a title id (or nothing) rather than something to show a person.

    build_library() falls back to the title id for a console-installed PS4 game this machine knows
    nothing else about - the PS5's app.db has no row for those. That fallback is fine as a last
    resort and wrong the moment anything better turns up, so every place that could supply a real
    name asks this first."""
    s = str(name or "").strip()
    if not s:
        return True
    if tid and s.upper() == str(tid).strip().upper():
        return True
    return bool(_TID_NAME_RE.match(s))


def build_library(srv):
    """PC-folder library (installable) merged with the console's installed games (real, from app.db)."""
    games = [dict(g) for g in srv.library.games]
    by_tid = {g["title_id"]: g for g in games if g.get("title_id")}
    # EVERY CONSOLE, not just the first one.
    #
    # This used to read consoles[0] alone, which was the whole fleet when the fleet was one PS5. With
    # a PS5 and a PS4 configured, that meant the second console's installed games were invisible -
    # the library showed them as not installed and offered to install them again. Each console is
    # asked in turn and the first one that has a title fills in the scalar fields, so a machine with
    # a single console behaves exactly as it did before; `installed_on` records all of them.
    fleet_bridges = [(cid, srv.fleet.bridge(cid)) for cid in srv.fleet.ids()]
    fleet_bridges = [(cid, b) for cid, b in fleet_bridges if b is not None]
    # (No `bridge` singular here any more. It held consoles[0] from when the fleet was one PS5;
    # once this loop was widened to every console nothing read it, and a name that looks like "the
    # console" while meaning "the first one" is the shape of bug this whole pass is about.)
    # ASKED AT THE SAME TIME, NOT ONE AFTER THE OTHER. console_apps() is a whole app.db pull on a
    # cold PS5 and an /api/library round trip on the PS4, and this loop paid them back to back, so
    # the first library load with both consoles cold - and every load right after an install drops
    # the memo - cost the SUM of the two. They are two independent questions about two different
    # machines, and the whole point of having two consoles is that they can be asked together.
    #
    # The warm path does not move at all: a fresh cache returns instantly, and a stale one returns
    # the old list while refreshing in its own background thread, exactly as before.
    #
    # THE RESULT IS REBUILT IN FLEET ORDER, NOT COMPLETION ORDER, because everything below relies
    # on "the first console that has a title fills in the scalar fields". That is also what keeps
    # a single-console fleet identical: one console is one thread and the same answer.
    apps = None
    per_console = []
    _got = {}

    def _ask_console(cid, b):
        try:
            _got[cid] = b.console_apps()
        except Exception:
            _got[cid] = None

    _threads = [threading.Thread(target=_ask_console, args=(cid, b), daemon=True)
                for cid, b in fleet_bridges]
    for _t in _threads:
        _t.start()
    # ONE DEADLINE FOR THE WHOLE FAN-OUT, not 75 s per thread: the threads run at the same time, so
    # a per-thread timeout would add up and make two wedged consoles cost twice what one does - the
    # very thing this change is removing. 75 s is past fs_read's own 60 s budget, so a slow app.db
    # pull still lands rather than being abandoned and reported as an empty console.
    _deadline = time.time() + 75.0
    for _t in _threads:
        _t.join(timeout=max(0.05, _deadline - time.time()))
    for cid, b in fleet_bridges:
        got = _got.get(cid)
        if got is None:
            continue
        per_console.append((cid, b, got))
        apps = (apps or []) + got
    for cid, _b, capps in per_console:
        for a in capps:
            if a["platform"] not in ("PS4", "PS5"):
                continue                       # skip system apps in the game library
            g = by_tid.get(a["title_id"])
            csize = a.get("size") or 0         # REAL installed size (tbl_contentinfo.size == AppInfoJson #_size)
            if g:                              # already in PC folder → enrich; keep the pkg file size as primary
                first = not g.get("on_console")
                g["on_console"] = True
                g.setdefault("installed_on", [])
                if cid not in g["installed_on"]:
                    g["installed_on"].append(cid)
                # WHICH CONSOLE, AND WHAT IT HAS THERE.
                #
                # installed_on already said WHERE a title is. It did not say what is there, and the
                # page needs that: the drive it sits on and the version installed are different
                # facts on each console, and the game panel was showing the first console's answer
                # whichever console the owner had chosen. Measured on the owner's fleet: 117 titles
                # with install state, 106 on the PS5 alone, 7 on the PS4 alone, 4 on both - so for
                # 113 of them the panel could name a drive that belongs to the other machine.
                #
                # A NEW key beside the scalars, never instead of them. The scalar fields keep
                # meaning exactly what they meant - the first console that has the title, which is
                # the PS5 on a fleet with both - so every existing reader, including an older page
                # served by a console that has not been rebuilt, sees no change at all.
                g.setdefault("console_state", {})[cid] = {
                    "drive": a.get("drive") or "",
                    "version": a.get("app_ver") or "",
                    "size": csize,
                    "backup_path": a.get("backup_path"),
                    "source": a.get("source") or "",
                }
                if first:
                    g["installed_drive"] = a["drive"]
                    g["installed_version"] = a.get("app_ver") or ""   # lets the UI hide applied patches
                    g["console_size"] = csize
                    g["backup_path"] = a.get("backup_path")
                    if a.get("source") == "backup":
                        g["size"] = a["size"]
                        g["format"] = a.get("format") or g.get("format")
                g["region"] = (g.get("region") if g.get("region") not in (None, "", "—") else a.get("region")) or "—"
                g["size_known"] = True
            else:                              # installed on console (not in the PC folder) → real console data
                games.append({"title_id": a["title_id"], "content_id": a.get("content_id", ""),
                              "name": a["name"], "platform": a["platform"],
                              "region": a.get("region", "—"), "lane": "installed", "on_console": True,
                              # Same per-console detail as the enrich branch above: a title that
                              # exists only on a console, and not in any PC folder, still has to be
                              # described per console. (`installed_on` is already set further down
                              # this same literal - setting it twice is not an error Python reports,
                              # the second one simply wins, which is exactly why it is worth not
                              # doing.)
                              "console_state": {cid: {"drive": a.get("drive") or "",
                                                      "version": a.get("app_ver") or "",
                                                      "size": csize,
                                                      "backup_path": a.get("backup_path"),
                                                      "source": a.get("source") or ""}},
                              "installed_drive": a["drive"], "installed_version": a.get("app_ver") or "",
                              "size": csize, "console_size": csize,
                              "size_known": csize > 0,
                              "format": a.get("format") or (a["platform"] + " app"),
                              "backup_path": a.get("backup_path"), "source": a.get("source"),
                              # A backup has a real container behind it, so it can be moved to
                              # another drive. "Move to another drive" is shown only for a title
                              # with `movable` AND a path, so without these the one control that
                              # puts a game on a different drive was hidden for precisely the
                              # games that have a file to move. Kept in step with the console's
                              # own library output.
                              "movable": bool(a.get("backup_path")),
                              "local_path": a.get("backup_path") or None,
                              "runs_in_place": bool(a.get("backup_path")),
                              "cover_seed": a["title_id"], "has_icon": bool(a.get("icon_path")),
                              "installed_on": [cid],
                              "base": [], "updates": [], "dlc": [], "cheats": []})
                by_tid[a["title_id"]] = games[-1]

    # Packages sitting on a USB stick plugged into the PS5. The console app finds these on
    # its own; without this the same stick would appear ONLY when the PC was switched off,
    # which is exactly the kind of inconsistency that makes the app feel unreliable.
    # Matched by title id so a stick copy never duplicates a title already listed.
    # Only worth asking when the console answered a moment ago. This call allows 20s for a
    # console that is genuinely busy scanning sticks, which is right when it is there and
    # completely wrong when it is not — it was the bulk of the ~25s the library took with the
    # PS5 off. `apps is not None` is the same reachability signal reported below.
    for _cid, b, _capps in per_console:
        try:
            for cg in (b.console_usb_packages() or []):
                tid = cg.get("title_id")
                if tid and tid in by_tid:
                    if not by_tid[tid].get("base"):
                        by_tid[tid]["base"] = cg.get("base") or []
                    continue
                games.append(cg)
                if tid:
                    by_tid[tid] = cg
        except Exception:
            pass

    games.sort(key=lambda g: g["name"].lower())
    return {"empty": len(games) == 0, "count": len(games),
            "console_reachable": apps is not None, "games": games}


def human_size(n):
    n = float(n or 0)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if n < 1024 or unit == "TB":
            return ("%.0f %s" % (n, unit)) if unit in ("B", "KB") else ("%.2f %s" % (n, unit))
        n /= 1024.0
    return "%.2f TB" % n


def device_identity(cfg):
    """Stable identity for this PC. Written to config once and reused forever: a hostname
    changes and is not unique, so peers need something they can key on to recognise the same
    machine after a restart, a rename, or a DHCP address change."""
    dev = cfg.setdefault("device", {})
    if not dev.get("id"):
        dev["id"] = uuid.uuid4().hex[:16]
        try:
            save_config(cfg)
        except Exception:
            pass                                   # a read-only config still works, just not sticky
    if not dev.get("name"):
        dev["name"] = socket.gethostname()
    return dev


def _library_capacity(srv):
    """Free/total of the drive this PC's library actually lives on.

    Every PC already knew this and never sent it, so a PC tile could only ever show a game count
    while a console drive showed free space - the same row of tiles saying two different things."""
    paths = (srv.cfg.get("library", {}) or {}).get("local_paths") or []
    for p in paths:
        try:
            import shutil
            du = shutil.disk_usage(p if os.path.isdir(p) else os.path.dirname(p) or p)
            return {"free": du.free, "total": du.total}
        except Exception:
            continue
    return {"free": None, "total": None}


def _library_counts(srv):
    ps4 = ps5 = 0
    nbytes = 0
    for g in srv.library.games:
        if (g.get("platform") or "") == "PS5":
            ps5 += 1
        elif (g.get("platform") or "") == "PS4":
            ps4 += 1
        # How much this PC is actually holding. Peers report it too, so every machine can be shown
        # on the storage bar with a real size instead of just a title count.
        for kind in ("base", "updates", "dlc"):
            for it in (g.get(kind) or []):
                nbytes += int(it.get("size") or 0)
    return {"ps4": ps4, "ps5": ps5, "total": len(srv.library.games), "bytes": nbytes}


def discover_peers(cfg, timeout=0.35, port=None):
    """Find other PKG MUTANT SHOP companions on this /24 by probing the companion port.
    Same approach as the PS5 scan: parallel connects, short timeout, then identify each
    responder so we never merge a library from something that is not one of ours."""
    port = port or cfg.get("companion", {}).get("port", 8710)
    ip = lan_ip()
    if "." not in ip:
        return []
    me = device_identity(cfg).get("id")
    base = ip.rsplit(".", 1)[0]
    found, lock = [], threading.Lock()

    def probe(i):
        host = "%s.%d" % (base, i)
        try:
            with socket.create_connection((host, port), timeout=timeout):
                pass
        except OSError:
            return
        # Generous on purpose. The port answering in milliseconds says nothing about how long the
        # peer takes to describe itself — it builds a library listing to do it, and an older build
        # may still probe its console first. 2.5s was under that: every scan saw the open port,
        # timed out identifying it, and threw the peer away. That alone is why PCs never found
        # each other unless the PS5 was up to introduce them.
        info = http_get_json("http://%s:%d/api/federation" % (host, port), timeout=8.0)
        # Only ours, and never ourselves (the same box answers on several addresses).
        if not info or not info.get("id") or info.get("id") == me:
            return
        info["lan_ip"] = info.get("lan_ip") or host
        info["url"] = "http://%s:%d" % (host, port)
        with lock:
            found.append(info)

    # A BOUNDED POOL, NOT 254 RAW THREADS. Every sweep created 254 OS threads and fired 254
    # simultaneous connects at the same /24 the consoles sit on - and it runs from a request path,
    # so a library load can trigger it once a minute while those same consoles are being polled
    # across the same link. 32 workers finish a /24 in about the same wall time, because the cost
    # is dominated by the 0.35 s connect timeout rather than by parallelism, at a fraction of the
    # socket and scheduler pressure.
    #
    # daemon=True still, and for the reason the PS5 scan below spells out: a probe stuck in a
    # connect must never be the thread that keeps the process alive after the tray's Quit. That is
    # also why this is a hand-rolled pool rather than concurrent.futures.ThreadPoolExecutor, whose
    # workers are non-daemon and are joined at interpreter exit.
    #
    # One deadline for the whole sweep, not four seconds per thread. Waiting per-thread meant a
    # slow-to-identify peer could still be mid-answer when its turn came and be lost anyway. Do
    # not stretch it: /api/library waits behind this sweep.
    deadline = time.time() + 12.0
    _next, _nlock = [1], threading.Lock()

    def _worker():
        while True:
            with _nlock:
                i = _next[0]
                _next[0] += 1
            if i > 254 or time.time() >= deadline:
                return
            try:
                probe(i)
            except Exception:
                pass

    threads = [threading.Thread(target=_worker, daemon=True) for _ in range(32)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=max(0.05, deadline - time.time()))
    return sorted(found, key=lambda x: x.get("name") or x.get("url"))


class PeerRegistry:
    """Known companions, refreshed in the background so the library merge never blocks on a
    LAN scan. Peers are remembered by device id, so a peer that changes address is still the
    same peer rather than a duplicate entry."""

    def __init__(self, cfg):
        self.cfg = cfg
        self.peers = {}                 # id -> info
        self.last_scan = 0
        self.scanning = False
        self.lock = threading.Lock()

    def known(self):
        with self.lock:
            return list(self.peers.values())

    def _drop_same_address_locked(self, pid, url, lan_ip_):
        """Forget any OTHER id that answers at this url/address. Caller holds self.lock.

        A peer's device id changes whenever its config.json is deleted or unreadable (save is
        refused, so device_identity() invents a fresh id every start). The old entry then stayed
        behind - online, same URL, same games - so the peer was merged twice: two hosts per title,
        two tiles with one label. One machine, one entry."""
        for old_id in [k for k, v in self.peers.items()
                       if k != pid and ((url and v.get("url") == url)
                                        or (lan_ip_ and v.get("lan_ip") == lan_ip_))]:
            print("[peers] %s re-identified as %s - dropping the old entry" % (old_id, pid))
            self.peers.pop(old_id, None)

    def _merge(self, infos):
        with self.lock:
            for info in infos:
                pid = info.get("id")
                if not pid:
                    continue
                prev = self.peers.get(pid, {})
                info["first_seen"] = prev.get("first_seen") or now_ms()
                info["last_seen"] = now_ms()
                info["online"] = True
                self._drop_same_address_locked(pid, info.get("url"), info.get("lan_ip"))
                self.peers[pid] = info
            self.last_scan = now_ms()

    def scan(self):
        infos = discover_peers(self.cfg)
        self._merge(infos)
        me = device_identity(self.cfg).get("id")
        for info in infos:                 # and whoever they can see, so the set converges
            self._adopt(info, me)
        return self.known()

    def _scan_guarded(self):
        """scan(), but never two at once. keepalive() used to call scan() straight, outside the
        in-flight flag scan_async() keeps, so a library load landing during the 5-minute sweep
        started a second 254-thread sweep on top of the first."""
        with self.lock:
            if self.scanning:
                return
            self.scanning = True
        try:
            self.scan()
        except Exception:
            pass
        finally:
            with self.lock:
                self.scanning = False

    def scan_async(self, min_interval_ms=60000):
        with self.lock:
            if self.scanning or (now_ms() - self.last_scan) < min_interval_ms:
                return
        threading.Thread(target=self._scan_guarded, daemon=True).start()

    def refresh_online(self):
        """Re-check the peers we already know, without a full /24 sweep."""
        me = device_identity(self.cfg).get("id")
        for pid, info in list(self.peers.items()):
            url = info.get("url")
            if not url:
                continue
            # Same budget as discovery: a peer describing itself is not a ping, and cutting it
            # short here would quietly mark a perfectly healthy machine as offline.
            fresh = http_get_json(url.rstrip("/") + "/api/federation", timeout=8.0)
            with self.lock:
                if fresh and fresh.get("id"):
                    fresh["url"] = url
                    fresh["first_seen"] = info.get("first_seen")
                    fresh["last_seen"] = now_ms()
                    fresh["online"] = True
                    if fresh["id"] != pid:
                        # Same machine, new id: the loop iterated the OLD id and stored under the
                        # NEW one, and the old entry was never marked offline - it stayed online
                        # with the same URL until this PC restarted.
                        self.peers.pop(pid, None)
                    self._drop_same_address_locked(fresh["id"], url, fresh.get("lan_ip"))
                    self.peers[fresh["id"]] = fresh
                elif pid in self.peers:
                    self.peers[pid]["online"] = False
            self._adopt(fresh, me)
        return self.known()

    def _adopt(self, info, me):
        """Take on the PCs a peer can see. Every companion advertises `known_pcs`, so one machine
        finding another is enough for the whole set to converge — no repeated /24 sweeps, and it
        keeps working when the PS5 (which used to do the introducing) is off."""
        if not info:
            return
        port = self.cfg.get("companion", {}).get("port", 8710)
        mine = lan_ip()
        for kp in (info.get("known_pcs") or []):
            ip = kp.get("lan_ip")
            if not ip or ip == mine:
                continue
            with self.lock:
                if any((p.get("lan_ip") == ip) for p in self.peers.values()):
                    continue
            got = http_get_json("http://%s:%d/api/federation" % (ip, int(kp.get("port") or port)),
                                timeout=8.0)
            if got and got.get("id") and got.get("id") != me:
                got["lan_ip"] = got.get("lan_ip") or ip
                got["url"] = "http://%s:%d" % (ip, int(kp.get("port") or port))
                self._merge([got])

    def keepalive(self, srv=None):
        """Keep the peer list true without anyone opening the app.

        Discovery used to happen only as a side effect of a request, so two PCs sitting idle never
        found each other at all. A full sweep is expensive, so it runs rarely; re-checking the
        machines we already know is cheap, so it runs often."""
        def loop():
            first = True
            while True:
                try:
                    if first or (now_ms() - self.last_scan) > 300000:   # full sweep at most every 5 min
                        self._scan_guarded()
                        first = False
                    else:
                        self.refresh_online()
                except Exception:
                    pass
                time.sleep(20)

        threading.Thread(target=loop, daemon=True).start()


def _with_peer_sources(srv, doc):
    """Fold the other PCs into the source list. They really ARE sources - the console can pull a
    game straight from them - and counting only download mirrors is why the header said
    "1 source" however many machines were connected."""
    try:
        reg = getattr(srv, "peers", None)
        peers = reg.known() if reg is not None else []
        srcs = list(doc.get("sources") or [])
        for p in peers:
            if not p.get("online"):
                continue
            c = p.get("counts") or {}
            srcs.append({"name": p.get("name") or p.get("lan_ip"), "ok": True, "kind": "peer",
                         "latency_ms": None, "titles": c.get("total", p.get("count", 0))})
        doc["sources"] = srcs
        doc["peers"] = [{"name": p.get("name"), "online": bool(p.get("online")),
                         "lan_ip": p.get("lan_ip")} for p in peers]
    except Exception:
        pass
    return doc


def _known_pc_addrs(srv):
    """Compact address list of the other companions we know about, for peer-of-peer discovery."""
    out = []
    try:
        reg = getattr(srv, "peers", None)
        for p in (reg.known() if reg is not None else []):
            ip = p.get("lan_ip")
            if not ip:
                url = p.get("url") or ""
                ip = url.split("//")[-1].split(":")[0] if "//" in url else ""
            if not ip:
                continue
            prt = p.get("companion_port") or srv.cfg["companion"]["port"]
            out.append({"lan_ip": ip, "port": int(prt), "online": bool(p.get("online"))})
    except Exception:
        pass
    return out


def federation_self(srv):
    """What this companion advertises to federation peers: its name + a compact list of its installable
    library, so another companion can present one merged library. [B9]"""
    dev = device_identity(srv.cfg)
    name = srv.cfg.get("federation", {}).get("name") or dev.get("name") or socket.gethostname()
    port = srv.cfg["companion"]["port"]
    # Once per call. lan_ip() opens, connects and closes a UDP socket; it was called twice per
    # game inside the loop below - 226 sockets per /api/federation, which every peer polls every
    # 20 s - for a value that cannot change between two lines of the same reply.
    ip = lan_ip()
    # WHAT THIS PC CAN HAND OVER FOR THE PAYLOADS PANEL. A second machine running this exe has no
    # copy of the owner's folder, so without this every tile there reads "not on this PC" and
    # nothing can be pressed - while the bytes sit on a console or on the PC next to it.
    try:
        payloads_have = payload_engine.fleet_summary(srv.cfg, WEB_DIR)
    except Exception:
        payloads_have = []
    games = []
    for g in srv.library.games:
        # Every installable file this PC holds for the title, tagged with what it is. Sending
        # only the base is what made add-ons invisible from other machines.
        items = []
        for kind, lst in (("base", g.get("base")), ("update", g.get("updates")), ("dlc", g.get("dlc"))):
            for it in (lst or []):
                if not it.get("install_key"):
                    continue
                items.append({"install_key": it["install_key"], "kind": it.get("kind") or kind,
                              "version": it.get("version"), "size": it.get("size", 0),
                              "file": it.get("file"), "content_id": it.get("content_id", ""),
                              # container type per file, so a backup stays identifiable as one
                              "format": it.get("format") or g.get("format"),
                              "parts": it.get("parts"), "multi_part": it.get("multi_part", False)})
        if not items:
            continue
        base = (g.get("base") or [{}])[0]
        games.append({"title_id": g.get("title_id"), "name": g.get("name"), "size": g.get("size"),
                      "platform": g.get("platform"), "region": g.get("region"),
                      "update_only": bool(g.get("update_only")),
                      # kept for older peers that only understand a single key
                      "install_key": base.get("install_key") or items[0]["install_key"],
                      "version": base.get("version"), "items": items,
                      # A ShadowMount backup (.ffpfs / .ffpfsc / .ffpkg …) is not a PKG: it mounts
                      # instead of installing, and its container type is the whole point. Neither
                      # travelled, so a backup held on another PC arrived looking like a package
                      # with no format at all — which is why its panel read "—" everywhere.
                      "format": g.get("format"),
                      # Cover art lives on whichever PC holds the game. Tell peers it exists and
                      # where to get it, or every other device falls back to drawing initials.
                      "has_icon": bool(g.get("has_icon")),
                      "thumb_url": ("http://%s:%d/thumb/%s.webp" % (ip, port, g.get("title_id")))
                                   if g.get("title_id") else "",
                      "icon_url": ("http://%s:%d/icon/%s.png" % (ip, port, g.get("title_id")))
                                  if (g.get("has_icon") and g.get("title_id")) else None})
    con = _first_bridge(srv)
    return {"id": dev.get("id"), "name": name, "hostname": socket.gethostname(),
            "os": platform.system(), "app": "PKG MUTANT SHOP", "version": VERSION,
            "lan_ip": ip, "companion_port": port,
            "url": "http://%s:%d" % (ip, port),
            "lan_url": "http://%s:%d/library/" % (ip, port),
            "console": {"ip": con.ip if con else None,
                        # read-only: this reply is how other PCs identify us, so it must
                        # never wait on the console. Health refreshes the value.
                        "online": bool(con and con.pldmgr.alive(probe=False))} if con else None,
            "counts": _library_counts(srv), "count": len(games), "payloads": payloads_have,
        "games": games,
            # So another machine can draw this PC's tile with the same four facts as its own.
            "storage": _library_capacity(srv),
            # Who else we can see. The console reads this and registers them itself, so one PC
            # finding the PS5 is enough for every PC to be found - no subnet sweep on the console.
            "known_pcs": _known_pc_addrs(srv)}


def build_federated_library(srv):
    """Local library merged with every peer companion's library, deduped by title id.

    A title can live on more than one PC, so each entry carries `hosts` — who has a copy and
    where to fetch it from. Peer-only titles are still installable: the console downloads
    straight from that PC, so nothing is proxied through this one.

    Peers come from two places: ones discovered on the LAN, and ones written into the config
    by hand. Discovery means a second PC only has to run the app to join."""
    result = build_library(srv)
    games = result["games"]
    by_tid = {g.get("title_id"): g for g in games if g.get("title_id")}
    me = device_identity(srv.cfg)

    # Everything local is hosted by THIS pc — say so, so the UI can show provenance
    # consistently instead of only labelling the remote ones.
    for g in games:
        if g.get("base") and any(b.get("install_key") for b in g["base"]):
            g.setdefault("hosts", []).append({"pc": me.get("name"), "id": me.get("id"),
                                              "local": True, "url": None})
            g.setdefault("source_pc", me.get("name"))
            g.setdefault("source_id", me.get("id"))

    fed = srv.cfg.get("federation", {})
    peers = []
    reg = getattr(srv, "peers", None)
    if reg is not None:
        reg.scan_async()                       # keeps itself fresh without blocking this request
        peers.extend(reg.known())
    # A CONFIGURED PEER THAT IS SWITCHED OFF COST UP TO 2.5 s ON EVERY CALL, uncached. That is
    # not only the grid: this function is also on the copy-from-another-PC lane and the peer
    # install lane, so a dead peer written into config.json slowed a press of Install too. Remember
    # the failure for 60 s and skip it, the way Ps5Bridge's _apps_fail_ts short-circuits a console
    # that has just failed. (2.5 s is the ceiling for a host that black-holes the connect; a host
    # that is up with the port closed answers RST in microseconds and was never what this cost.)
    _fail = getattr(srv, "_fed_fail_ts", None)
    if _fail is None:
        _fail = srv._fed_fail_ts = {}
    # Compared WITHOUT a trailing slash. The registry stores urls as exactly http://ip:port, so a
    # config entry written with a trailing slash matched nothing here and the same machine was
    # probed a second time and merged into the list twice.
    seen_urls = {(p.get("url") or "").rstrip("/") for p in peers if p.get("url")}
    for peer in fed.get("peers", []):          # hand-configured peers still work
        purl = peer if isinstance(peer, str) else (peer.get("url") if isinstance(peer, dict) else None)
        if not purl:
            continue
        key = purl.rstrip("/")
        if key in seen_urls:
            continue
        seen_urls.add(key)
        if time.time() - _fail.get(key, 0) < 60:
            peers.append({"url": purl, "name": purl, "online": False, "games": [], "count": 0})
            continue
        info = http_get_json(key + "/api/federation", timeout=2.5)
        if info:
            _fail.pop(key, None)               # it answered: stop holding the failure against it
            info["url"] = purl
            info["online"] = True
            peers.append(info)
        else:
            _fail[key] = time.time()
            peers.append({"url": purl, "name": purl, "online": False, "games": [], "count": 0})

    peers_status = []
    for info in peers:
        purl = info.get("url") or ""
        peers_status.append(peer_summary(info))
        if not info.get("online"):
            continue
        base_url = info.get("lan_url") or (purl.rstrip("/") + "/library/")
        try:
            _merge_peer_games(info, base_url, games, by_tid)
        except Exception as e:
            # One peer's odd document must not take the LOCAL library down for every device
            # (the PS5's page included). A peer answering games:null, a non-dict game or a
            # numeric install_key used to raise here and /api/library failed until it went away.
            print("[peers] skipping %s's library: %r" % (info.get("name") or purl, e))

    # CARD ART: point every local title at its card-sized thumbnail.
    #
    # This is the single most expensive omission in the whole app, and it was invisible because
    # each half looked right on its own. /api/federation has always advertised thumb_url (it is
    # built ~140 lines above), and the console's own /api/library sets it on 112 of 113 titles.
    # But the PS5 does not read either of those: the page is served from the console, then
    # resolveApi() upgrades API to a PC companion, so every cover the console paints comes from
    # THIS endpoint - and this endpoint never set the field. iconUrl() in the UI prefers
    # thumb_url, finds none, falls through icon_url, and lands on the full-size PNG.
    #
    # Measured across a 10-title sample: 415,866 bytes average per cover instead of 26,409 - a
    # 15.7x difference on the wire - and 1,048,576 bytes of decoded RGBA per cover instead of
    # 409,600, held for every card in the DOM at once. On the first screen alone that is roughly
    # 20 MB of PNG and ~55 MB of bitmap. The thumbnails already existed on this very host.
    #
    # Only ever fill in a blank: a peer-hosted title already carries its owner's URL above, and
    # rewriting that to point here would serve art we do not have.
    _port = int((srv.cfg.get("companion") or {}).get("port") or 8710)
    _ip = lan_ip()
    for g in games:
        if g.get("thumb_url") or not g.get("title_id") or not g.get("has_icon"):
            continue
        g["thumb_url"] = "http://%s:%d/thumb/%s.webp" % (_ip, _port, g["title_id"])

    games.sort(key=lambda g: (g.get("name") or "").lower())
    result["games"] = games
    result["count"] = len(games)
    result["peers"] = peers_status
    result["device"] = {"id": me.get("id"), "name": me.get("name")}
    return result


def peer_summary(info):
    """The compact, UI-facing description of one peer. One shape, used by /api/library's
    `peers` and by /api/federation/peers, so the two cannot drift apart."""
    purl = info.get("url") or ""
    return {"url": purl, "id": info.get("id"), "name": info.get("name") or purl,
            "online": bool(info.get("online")), "count": info.get("count", 0),
            "counts": info.get("counts"), "lan_ip": info.get("lan_ip"),
            "os": info.get("os"), "version": info.get("version"),
            "last_seen": info.get("last_seen")}


def _merge_peer_games(info, base_url, games, by_tid):
    """Fold one ONLINE peer's advertised games into `games`/`by_tid` (mutated in place).

    Split out of build_federated_library() so a malformed document from one peer can be caught
    per peer. Shapes are coerced on the way in rather than trusted: `games` must be a list, each
    game a dict, each install_key a string."""
    pgames = info.get("games")
    if not isinstance(pgames, list):
        return
    for pg in pgames:
        if not isinstance(pg, dict):
            continue
        tid = pg.get("title_id")
        if tid is not None and not isinstance(tid, str):
            tid = str(tid)
        host = {"pc": info.get("name"), "id": info.get("id"), "local": False,
                "url": base_url + str(pg.get("install_key") or "")}
        # Older peers send one key; newer ones send every file with its kind.
        pitems = pg.get("items") if isinstance(pg.get("items"), list) else None
        pitems = [i for i in (pitems or []) if isinstance(i, dict)] or \
            [{"install_key": pg.get("install_key"), "kind": "base",
              "version": pg.get("version"), "size": pg.get("size", 0),
              "file": pg.get("name")}]

        def _mk(it):
            return {"install_key": str(it.get("install_key") or ""), "file": it.get("file") or pg.get("name"),
                    "size": it.get("size", 0), "kind": it.get("kind") or "base",
                    "version": it.get("version"), "content_id": it.get("content_id", ""),
                    "format": it.get("format") or pg.get("format"),
                    "parts": it.get("parts"), "multi_part": it.get("multi_part", False),
                    "peer_url": base_url + str(it.get("install_key") or "")}

        g = by_tid.get(tid) if tid else None
        if g:
            # Same title on another PC. Record the extra source and fold in any file this
            # machine does not have — that is how a peer's DLC reaches the game's panel.
            if not any(h.get("id") == host["id"] for h in g.get("hosts", [])):
                g.setdefault("hosts", []).append(host)
            for it in pitems:
                if not it.get("install_key"):
                    continue
                bucket = {"base": "base", "update": "updates", "dlc": "dlc"}.get(it.get("kind") or "base", "base")
                have = {x.get("install_key") for x in (g.get(bucket) or [])}
                if it["install_key"] not in have:
                    g.setdefault(bucket, []).append(_mk(it))
            if g.get("base"):
                g["update_only"] = False
            # A TITLE ID IS NOT A NAME. This title can already be here as a console-only
            # entry whose name fell back to the id (build_library, via server.py "name": tid)
            # because the PS5's app.db carries no row for an installed PS4 game. The peer that
            # holds the package knows what it is actually called - pg["name"] is the game name,
            # the filename lives in items[].file - and we are about to borrow its cover anyway.
            # One direction only: a placeholder is upgraded, a real name is never overwritten.
            if is_placeholder_name(g.get("name"), tid) and not is_placeholder_name(pg.get("name"), tid):
                g["name"] = pg["name"]
            if not g.get("has_icon") and pg.get("icon_url"):
                g["has_icon"] = True                 # borrow the peer's artwork
                g["icon_url"] = pg["icon_url"]
                if pg.get("thumb_url"):
                    g["thumb_url"] = pg["thumb_url"]   # and its card-sized copy
            continue

        buckets = {"base": [], "updates": [], "dlc": []}
        for it in pitems:
            if not it.get("install_key"):
                continue
            buckets[{"base": "base", "update": "updates", "dlc": "dlc"}.get(it.get("kind") or "base", "base")].append(_mk(it))
        # Carry the container type and version the owner reported. Falling back to the file
        # extension matters: a peer on an older build advertises no format at all, and the
        # filename is proof enough — so backups from a PC that has not been updated still
        # identify themselves correctly.
        pfmt = (pg.get("format") or (pitems[0].get("format") if pitems else None)
                or fmt_from_filename(pg.get("name"))
                or (fmt_from_filename(pitems[0].get("file")) if pitems else None))
        # A backup is a backup wherever it happens to sit: it mounts, so it gets the mount lane
        # and with it the drive picker, exactly as a local one does. The peer-mount lane fetches
        # it here first; every container in MOUNT_EXTS behaves the same way.
        pbackup = any(is_backup_item(i, pg) for i in pitems) if pitems else is_backup_item({}, pg)
        ng = {"title_id": tid, "name": pg.get("name"), "platform": pg.get("platform", "PS4"),
              "region": pg.get("region") or "—", "size": pg.get("size", 0),
              "lane": "mount" if pbackup else "install",
              "format": pfmt, "version": pg.get("version"),
              "size_known": bool(pg.get("size")),
              "cover_seed": tid or pg.get("name"),
              "has_icon": bool(pg.get("icon_url")), "icon_url": pg.get("icon_url"),
              # The owner serves its own art: never rewrite this to point at us.
              "thumb_url": pg.get("thumb_url") or "",
              "source_pc": info.get("name"), "source_id": info.get("id"), "remote": True,
              "update_only": (not buckets["base"]) or bool(pg.get("update_only")),
              "base": buckets["base"], "updates": buckets["updates"], "dlc": buckets["dlc"],
              "cheats": [], "hosts": [host]}
        games.append(ng)
        if tid:
            by_tid[tid] = ng



def _drive_of_backup(path):
    """Which console drive a container actually sits on, from its own path. app.db cannot answer
    this - it records where the TITLE is registered, not where the file is."""
    p = str(path or "")
    m = re.match(r"^/mnt/(ext[0-2]|usb[0-7])/", p)
    if m:
        return m.group(1)
    if p.startswith("/data/"):
        return "internal"
    return ""


def build_storage(srv, apps, devices=None, console_online=True):
    """What is actually holding games right now — console drives AND the PCs.

    Only live, populated places are listed. Every configured drive used to be emitted whenever the
    console was unreachable, which filled the bar with a row of identical "0 games" tiles that said
    nothing: eleven slots for storage that either was not there or held nothing. A drive earns its
    place by having something on it.

    The PCs belong here for the same reason the drives do — they are where games live, and the
    console installs straight from them. Each is listed with its real title count and size.

    `console_online` is what decides whether the console drives appear at all, and it is NOT the
    same question as "do we have an app list". console_apps() keeps serving its last good list for
    the lifetime of the process, which is correct for the library — browsing your games with the
    PS5 off is the point of it — but a storage bar is a live readout of what is plugged in. Drawing
    it from a remembered app.db left four tiles up with no capacity behind them, all reading
    "space unknown", and with the capacity map empty the loc:2 fold below could not run either, so
    the M.2 came back as two tiles. A drive is shown when the console is answering, and not before.
    """
    drives = []
    if apps is not None and console_online:
        # REAL capacity, straight from the console's statvfs. Without it a tile can only show a
        # used figure with nothing to judge it against - which is how "3352 GB" sat unquestioned on
        # a 2 TB drive.
        cap = {}
        for d in (devices or []):
            if d.get("detected") and d.get("total"):
                cap[str(d.get("id"))] = {"label": d.get("label") or str(d.get("id")),
                                         "free": d.get("free"), "total": d.get("total")}
        used, cnt, label = {}, {}, {}

        def _add(key, nice, size):
            used[key] = used.get(key, 0) + int(size or 0)
            cnt[key] = cnt.get(key, 0) + 1
            label.setdefault(key, nice)

        for a in apps:
            if a["platform"] not in ("PS4", "PS5"):
                continue
            # A backup lives wherever its container is, which app.db does not know. Only fall back
            # to app.db's location code when there is no container to look at.
            key = _drive_of_backup(a.get("backup_path"))
            if key:
                _add(key, (cap.get(key) or {}).get("label") or key.upper(), a.get("size"))
            elif a.get("location"):
                loc = a["location"]
                _add("loc:" + loc, Ps5Bridge.LOC_LABEL.get(loc, "Storage " + loc), a.get("size"))

        # FOLD THE app.db BUCKETS INTO THE REAL DRIVES THEY NAME. A title with a container is
        # bucketed by where that container actually is; one without falls back to app.db's location
        # code. Both can describe the SAME physical drive - which is why the M.2 appeared twice,
        # once as "ext1" with its 33 backups and once as "loc:2" with its 6 packages.
        #   location "0" -> the internal drive
        #   location "2" -> whichever extended drive is actually present
        _ext = next((i for i in ("ext1", "ext0", "ext2") if i in cap), None)
        for src, dst in (("loc:0", "internal"), ("loc:2", _ext)):
            if src in used and dst and dst in cap:
                used[dst] = used.get(dst, 0) + used.pop(src)
                cnt[dst] = cnt.get(dst, 0) + cnt.pop(src)
                label.setdefault(dst, (cap.get(dst) or {}).get("label") or dst)

        for key in sorted(used):
            if not cnt.get(key):
                continue
            c = cap.get(key) or {}
            if not c and key == "loc:0":
                c = cap.get("internal") or {}
            # A DRIVE EARNS ITS TILE BY BEING PLUGGED IN. These buckets are built from app.db and
            # from container paths, and neither notices hardware leaving: pull a stick and app.db
            # still lists its titles; remove the M.2 and rows still say location "2", which the
            # fold above then has no real drive to merge into. Either way the bucket has no
            # capacity behind it and used to render as a tile reading "space unknown" - or, in the
            # second case, as a second tile for a drive that already had one. `cap` holds only what
            # the console just reported as detected, so this is the whole test.
            if not c:
                continue
            # THE FILESYSTEM OVER app.db. `used[key]` is the sum of each title's app.db size, and
            # app.db's size is metadata - it claimed 790.9 GB of games on a 673.9 GB drive. When the
            # console has given us a real statvfs pair, total-free is what is actually occupied.
            real = used[key]
            if c.get("total") and c.get("free") is not None:
                real = max(0, int(c["total"]) - int(c["free"]))
            drives.append({"id": key, "label": label.get(key, key), "used": real,
                           "games_bytes": used[key],
                           "free": c.get("free"), "total": c.get("total"),
                           "count": cnt[key], "kind": "console"})

    # This PC, then every peer that is online and actually holding something.
    me = device_identity(srv.cfg)
    mine = _library_counts(srv)
    if mine.get("total"):
        _capme = _library_capacity(srv)
        drives.append({"id": "pc:" + str(me.get("id")), "label": me.get("name") or "This PC",
                       "used": mine.get("bytes") or None,
                       "free": _capme.get("free"), "total": _capme.get("total"),
                       "count": mine.get("total", 0), "kind": "pc", "local": True})
    reg = getattr(srv, "peers", None)
    for p in (reg.known() if reg is not None else []):
        if not p.get("online"):
            continue
        c = p.get("counts") or {}
        n = c.get("total", p.get("count", 0)) or 0
        if not n:
            continue
        # A peer on an older build reports no byte total. It still lists every file it holds, so
        # add them up here rather than showing that machine as an empty tile.
        nb = c.get("bytes")
        if not nb:
            nb = sum(int(it.get("size") or 0)
                     for g in (p.get("games") or []) for it in (g.get("items") or []))
        # A peer that advertises its library drive gets the same four facts as every other tile.
        # An older peer sends no "storage" block; its tile simply has no capacity to show, rather
        # than the whole row of tiles each showing a different thing.
        _st = p.get("storage") or {}
        drives.append({"id": "pc:" + str(p.get("id")), "label": p.get("name") or p.get("lan_ip"),
                       "used": nb or None,
                       "free": _st.get("free"), "total": _st.get("total"),
                       "count": n, "kind": "pc", "local": False, "lan_ip": p.get("lan_ip")})

    # PC tiles in one order everywhere. They were painted in answer order - this PC first, then
    # peers as discovered - so the same network read "ASUS-LAP, Casita" on one device and
    # "Casita, ASUS-LAP" on the next. "This PC" is a label the browser adds, not a position.
    pcs = sorted((d for d in drives if d.get("kind") == "pc"),
                 key=lambda d: (str(d.get("label") or "").lower(), str(d.get("id"))))
    drives = [d for d in drives if d.get("kind") != "pc"] + pcs

    return {"reachable": apps is not None and console_online, "drives": drives}


def _identifies_as_console(host, port=8710, timeout=1.6, out=None):
    """Does this host say IT IS a console? Returns True / False / None (could not ask).

    Pass `out` as a dict to also receive what it said about itself - today just "platform", which
    the PS4 payload reports and the PS5 payload does not (so a console that names no platform is a
    PS5). Additive: every existing caller reads only the return value.

    An open port cannot answer this. A peer PC running the companion listens on the same 8710 and
    speaks the same API; the only difference is what it says about itself. Our on-console ELF sets
    on_console true and server "on-console"; the companion does not."""
    try:
        with urllib.request.urlopen("http://%s:%d/api/health" % (host, port), timeout=timeout) as r:
            h = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    if not isinstance(h, dict):
        return None
    is_console = bool(h.get("on_console")) or h.get("server") == "on-console"
    if isinstance(out, dict) and is_console:
        seen = str(h.get("platform") or "ps5").strip().lower()
        out["platform"] = seen if seen in ("ps4", "ps5") else "ps5"
    return is_console


def discover_ps5(cfg):
    """Scan the local /24 for a console. Only hosts that identify themselves as one qualify.

    Finds a PS4 as readily as a PS5 - both run our shop on 8710 and both answer /api/health with
    on_console - and each result carries the platform the host reported, so a discovered PS4 is not
    saved as a PS5."""
    ip = lan_ip()
    if "." not in ip:
        return []
    base = ip.rsplit(".", 1)[0]
    # Both FTP ports, not just the configured one: a console running etaHEN serves FTP on 1337 and
    # 2121 is closed, so scanning only the configured port found the console solely by its DPI port
    # - and missed it completely whenever the install host happened to be down.
    ports = tuple(dict.fromkeys(
        (8710, 8084, cfg.get("ftp", {}).get("port", 2121), 2121)))
    found, lock = [], threading.Lock()

    def probe(i):
        host = "%s.%d" % (base, i)
        openp = []
        for p in ports:
            try:
                with socket.create_connection((host, p), timeout=0.35):
                    openp.append(p)
            except OSError:
                pass
        if not openp:
            return
        # ASK, do not assume. Without this a peer PC running the companion was indistinguishable
        # from the console and could be adopted as one - and then written to config.json.
        said = {}
        says = _identifies_as_console(host, out=said) if 8710 in openp else None
        if says is False:
            return                      # it told us it is not a console. Believe it.
        # If it would not answer, fall back to a fingerprint only a PS5 has: Payload Manager on
        # 8084. A peer PC runs the companion on 8710 and nothing on 8084, so requiring 8084 keeps
        # a slow-to-answer peer from being mistaken for a console that is merely busy.
        if says is None and 8084 not in openp:
            return
        with lock:
            found.append({"ip": host, "ports": openp, "confirmed": bool(says),
                          "platform": said.get("platform") or ""})

    # daemon=True, as discover_peers' are: a probe stuck in a connect must never be the thread
    # that keeps the process alive after the tray's Quit, and 254 of them were not.
    threads = [threading.Thread(target=probe, args=(i,), daemon=True) for i in range(1, 255)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3.0)
    # A confirmed console always outranks one we merely could not rule out, so the arbitrary
    # lowest-address tiebreak can no longer put a PC ahead of the real console.
    return sorted(found, key=lambda x: (0 if x.get("confirmed") else 1, x["ip"]))

# States in which a worker thread is actively driving the task. "reloading" belongs here: the
# worker is inside ensure_dpi_ready()/recover_dpi() for up to 75s, and leaving it out let _claim()
# start another job past max_parallel while that one was still in flight.
RUNNING = {"claimed", "verifying", "submitting", "transferring", "promoting", "reloading"}
# Anything that installs ON TOP of a title that is already there. These cannot be verified the way
# a base game is: the parent is already registered and its app.pkg is already full size, so both of
# those checks pass before the add-on has done anything at all.
ADDON_KINDS = {"update", "patch", "dlc", "backport"}


def _ver_key(v):
    """"01.04" -> (1, 4) so versions compare numerically. Empty/odd input -> ()."""
    parts = re.findall(r"\d+", str(v or ""))
    return tuple(int(x) for x in parts) if parts else ()
# bgft.db tbl_downloads.status values that mean the transfer+install finished.
# 1036 = a full title install, 1026 = a patch/update install. Anything else is in flight or parked.
BGFT_DONE = {1026, 1036}
# QUEUED-BUT-DEAD. status 1000 with length 0 and nothing transferred is a job the console accepted
# and never started - typically submitted when the target drive was full. It does not progress, it
# does not fail, and it blocks everything behind it. Treating it as "already installing" is what
# turned one stuck download into every install being refused before it was tried.
def bgft_row_is_dead(row):
    """Is this bgft row a job that was accepted and never started?

    row is (status, transferred, length, ...) as install_job_row() returns it. Status 1000 means
    queued - which covers both "about to start" and "will never start". The pair that separates
    them is length 0 AND transferred 0: a job the console has actually taken up knows how big it
    is. Seven of these, queued while the drive was full, blocked every install on this console."""
    try:
        status, transferred, length = int(row[0]), int(row[1] or 0), int(row[2] or 0)
    except Exception:
        return False
    return status == 1000 and transferred == 0 and length == 0
# Terminal statuses that are NOT success. 1021 is what a rejected package leaves behind - observed
# on this console as `status=1021 err=0x80B21104`. Used to end the confirm wait on evidence rather
# than on a stopwatch; see [11] in _run.
BGFT_FAILED = {1021, 1022, 1023}
# ShadowMount backups are MOUNTED, not installed - they never appear in bgft.db at all, which is
# why the install-confirm logic below must not be pointed at them. .ffpfsc = compressed PFS container.
# Every container ShadowMount can mount, plus raw images. The scan walks ALL subfolders.
MOUNT_EXTS = (".ffpfsc", ".ffpkg", ".ffpfs", ".exfat", ".ffpfsx", ".fpkg", ".iso", ".img")


class _Cancelled(Exception):
    pass


class _FsListing(list):
    """What fs_list() returns: a plain list of entries that also knows whether it is COMPLETE.

    The console caps a listing at 96000 bytes (~1265 entries) and says so with "truncated":true.
    fs_list() used to drop that flag on the floor, so a 2142-file cheat folder came back as 1265
    names and everything after them was "missing" - re-sent every 15 minutes, forever. A reader
    that computes a difference against a directory must check `truncated` first."""
    truncated = False


# One sync at a time, process-wide. The 15-minute thread and POST /api/cheats/sync both call
# sync_cheat_library(); overlapping them only doubles the uploads, but doubling 2000 uploads on
# the link an install is using is not nothing. Module-level rather than per bridge because
# Fleet.reload() (POST /api/config) builds fresh bridges while an old sync may still be running.
_CHEAT_SYNC_LOCK = threading.Lock()


def _mkdirs_list(path):
    parts, cur, out = [p for p in path.split("/") if p], "", []
    for p in parts:
        cur += "/" + p
        out.append(cur)
    return out


# --------------------------------------------------------------------------- #
# config / net helpers                                                         #
# --------------------------------------------------------------------------- #
# Keys that are subscripted directly somewhere in this file (cfg["companion"]["port"] and
# friends). If an override replaces one of these with null or a scalar, startup dies with a
# TypeError before anything is logged - and in the frozen exe that is a window that closes with no
# message at all. POST /api/config deep-merges an arbitrary unvalidated body, so this is reachable
# from a settings-panel bug or a hand-crafted request, not only from a hand edit.
_CONFIG_DICT_SECTIONS = ("dpi", "ftp", "console", "shadowmount", "cheats", "install", "library",
                         "queue", "companion", "maintenance", "performance", "integrity",
                         "federation", "device")


def coerce_config(cfg):
    """Put back any top-level section that an override turned into null or a scalar.

    Deliberately shallow and quiet: this exists so a bad value cannot stop the app from starting,
    not to validate the whole config. It says what it repaired, then carries on.
    """
    defaults = DEFAULT_CONFIG
    for k in _CONFIG_DICT_SECTIONS:
        if not isinstance(cfg.get(k), dict):
            was = cfg.get(k)
            cfg[k] = json.loads(json.dumps(defaults.get(k, {})))
            print("[cfg] section %r was %r, not an object - using defaults for it" % (k, was))
    for k in ("consoles", "sources", "drives"):
        if not isinstance(cfg.get(k), list):
            cfg[k] = json.loads(json.dumps(defaults.get(k, [])))
            print("[cfg] %r was not a list - using the default" % k)
    try:
        cfg["companion"]["port"] = int(cfg["companion"].get("port") or 8710)
    except (TypeError, ValueError):
        print("[cfg] companion.port was not a number - using 8710")
        cfg["companion"]["port"] = 8710
    reconcile_consoles(cfg)
    return cfg


def reconcile_consoles(cfg):
    """Keep cfg["consoles"] in step with the ps5_ip / ps4_ip settings.

    The settings panel asks for a PS5 address and a PS4 address, because which console you own is
    the one thing a person really has to tell this app. What everything downstream runs on, though,
    is a LIST - so the two fields are folded into it here, in one place, instead of the UI having to
    build fleet entries and the queue having to guess what they mean.

    Rules, in order:
      * an address that is set gets (or updates) an entry with the stable id "ps5" / "ps4", so a
        console selection stored in the UI or a held queue job keeps pointing at the same console
        across saves;
      * an address that is cleared removes only that platform's entry;
      * every OTHER entry is left exactly as it is - a second PS5, or a console someone added to
        config.json by hand, is not this function's business.

    Returns True when it changed anything.
    """
    cons = cfg.get("consoles")
    if not isinstance(cons, list):
        cons = cfg["consoles"] = []
    before = json.dumps(cons, sort_keys=True)
    ftp_port = (cfg.get("ftp") or {}).get("port", 2121)
    for plat, key, label in (("ps5", "ps5_ip", "PS5"), ("ps4", "ps4_ip", "PS4")):
        ip = str(cfg.get(key) or "").strip()
        # WHICH ENTRY THIS SETTING OWNS - and it owns exactly one.
        #
        # This used to claim EVERY entry whose platform matched, which contradicts the rule written
        # above it ("every OTHER entry is left exactly as it is"). Clearing the PS5 address removed
        # a second PS5 somebody had added by hand, and internal/SETUP-REMOTE.md tells people to add entries
        # with their own ids. The id is the stable name this function created, so the id is what it
        # looks for; a config written before ids existed is matched on platform instead, and only
        # when it has no id of its own to be known by.
        mine = [c for c in cons
                if isinstance(c, dict) and str(c.get("id") or "").lower() == plat]
        if not mine:
            mine = [c for c in cons
                    if isinstance(c, dict) and not c.get("id")
                    and str(c.get("platform") or "").lower() == plat]
        if not ip:
            for c in mine:
                cons.remove(c)
            continue
        if mine:
            mine[0]["ip"] = ip
            mine[0]["platform"] = plat
            mine[0].setdefault("name", label)
            mine[0].setdefault("ftp_port", ftp_port)
            continue
        # ONE PHYSICAL CONSOLE, ONE ENTRY. An entry someone wrote by hand - {"id":"living"} with no
        # platform - matched neither test above, so this appended a SECOND entry for the same
        # address. That is not merely untidy: the install queue serialises by console ID, so one
        # console under two ids defeats "one install at a time per console" and runs two installs
        # against the same box at once. If the address is already in the list, that entry IS this
        # console; fill in what it is missing and leave its id alone.
        same = [c for c in cons
                if isinstance(c, dict) and str(c.get("ip") or "").strip() == ip]
        if same:
            same[0].setdefault("platform", plat)
            same[0].setdefault("name", label)
            same[0].setdefault("ftp_port", ftp_port)
        else:
            cons.append({"id": plat, "name": label, "ip": ip,
                         "platform": plat, "ftp_port": ftp_port})
    # PS5 first, so the many places that still mean "the console" by consoles[0] keep meaning the
    # PS5 on a machine that has both. Everything unrecognised keeps its relative order at the end.
    # THE SAME RULE THE REST OF THIS FILE USES: a console that does not say what it is, is a PS5.
    # This ranked ps5=0, ps4=1 and EVERYTHING ELSE=2, so an entry added to config.json by hand -
    # the exact shape internal/SETUP-REMOTE.md section 3 tells people to write, {"id":"living","ip":...}
    # with no platform field - sorted BEHIND the PS4 and made consoles[0] the PS4. Every place that
    # still means "the console" by consoles[0] then described the PS4: _bridge_for's final
    # fallback (so /api/storage and /api/console/apps), /api/federation, the queue's pc-copy
    # default target and _console_for_request. The test is `== "ps4"`, never `== "ps5"`, and
    # list.sort is stable, so PS5s and unstated consoles keep their configured order among
    # themselves and an ordinary ps5/ps4 config comes out exactly as it does today.
    cons.sort(key=lambda c: 1 if str((c or {}).get("platform") or "").lower() == "ps4" else 0)
    return json.dumps(cons, sort_keys=True) != before


def deep_merge(base, over):
    out = dict(base)
    for k, v in (over or {}).items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _adopt_library_root(cfg):
    """Guarantee the canonical library root exists and is scanned.

    Users drop games in C:\Mutant Games\PS4 (or PS5); the app must find them with no setup. We
    create the tree if missing and make sure the root is in local_paths. Any previously configured
    folder is KEPT (so an existing library keeps working) but the canonical root always wins first
    place."""
    lib = cfg.setdefault("library", {})
    paths = [p for p in (lib.get("local_paths") or []) if p]
    if sys.platform != "win32":
        # "C:\Mutant Games" is a Windows path. On Linux/macOS (start.sh, a home box) makedirs()
        # created a literal folder called "C:\Mutant Games" inside companion/ and then scanned
        # it. The canonical root is a Windows convenience; elsewhere the configured paths rule.
        lib["local_paths"] = paths
        return cfg
    norm = os.path.normcase(os.path.normpath(LIBRARY_ROOT))
    if not any(os.path.normcase(os.path.normpath(p)) == norm for p in paths):
        paths.insert(0, LIBRARY_ROOT)
    lib["local_paths"] = paths
    try:
        ensure_library_tree(LIBRARY_ROOT)
    except Exception:
        pass
    return cfg


# Set when config.json exists but could not be parsed. While this is true, saving is REFUSED:
# see save_config(). It is module state rather than a flag on the dict because the dict is copied,
# deep-merged and handed around, and the one thing that must not be lost is "we are running on
# defaults because we could not read the user's file".
_CONFIG_UNREADABLE = None


def load_config():
    global _CONFIG_UNREADABLE
    _CONFIG_UNREADABLE = None
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    # DEFAULT_CONFIG, then the user's config.json - and nothing in between. config.example.json
    # used to be layered underneath as a live third source, so a SOURCE run inherited whatever the
    # example happened to say (ShadowMount on 9021, a dead library root, dpi.mode "v2") even with
    # a minimal config.json, and "deleting a key inherits the example, not the default" had to be
    # documented as if it were intended. The example is a template for people to copy; the app
    # reads only its own defaults and the one file the user owns.
    for path in (CONFIG_PATH,):
        if os.path.exists(path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                for k in list(data):
                    if k.startswith("_"):
                        data.pop(k)
                cfg = deep_merge(cfg, data)
                # ONE-TIME: the old shipped placeholder becomes empty again.
                # Until this version the default address WAS "192.168.1.50", and save_config writes
                # the merged dict - so a config.json written by an older build very likely carries
                # that literal even though nobody ever typed it. Now that the default is empty, that
                # value would be read as a real console and a PS4-only owner would acquire a dead
                # PS5. Dropped once, recorded in config_rev, and never touched again: if someone
                # genuinely has a console at that address they can type it and it will stick.
                if int(cfg.get("config_rev") or 0) < 1:
                    if str(cfg.get("ps5_ip") or "").strip() == "192.168.1.50":
                        cfg["ps5_ip"] = ""
                        print("[cfg] the old example address 192.168.1.50 was still in config.json "
                              "- clearing it once; set your PS5's real address in Settings")
                    cfg["config_rev"] = 1
            except Exception as e:
                print("[cfg] could not read %s: %s" % (path, e))
                if path == CONFIG_PATH:
                    # A truncated config.json (power cut, editor crash) used to be silently
                    # replaced by defaults - and then, because the defaults point at 192.168.1.50,
                    # main()'s auto-discover branch called save_config() and WROTE THE DEFAULTS
                    # OVER THE USER'S FILE. Their library paths, sources and console list were
                    # gone, and nothing had said a word.
                    #
                    # Keep a copy of what could not be read, and refuse to save until someone
                    # deliberately overrides. Running on defaults for one session is recoverable;
                    # overwriting the only copy is not.
                    _CONFIG_UNREADABLE = str(e)
                    try:
                        keep = "%s.unreadable-%s" % (CONFIG_PATH, time.strftime("%Y%m%d-%H%M%S"))
                        shutil.copy2(CONFIG_PATH, keep)
                        print("[cfg] kept a copy of the unreadable config at %s" % keep)
                    except Exception as ce:
                        print("[cfg] could not preserve the unreadable config: %r" % (ce,))
                    print("[cfg] RUNNING ON DEFAULTS and refusing to save - fix or delete %s"
                          % CONFIG_PATH)
    cfg = coerce_config(cfg)
    # One value that outlived the code that wrote it. save_config() persists the whole merged
    # dict, so a config.json written when the ShadowMount default was 9021 (elfldr's port - see
    # DEFAULT_CONFIG) carries 9021 for ever, and helper_status() reads it whenever the console
    # is not there to correct it. In memory only - the file is the user's and is not rewritten
    # for this; the next deliberate save carries the corrected value.
    try:
        if int((cfg.get("shadowmount") or {}).get("port") or 0) == 9021:
            cfg["shadowmount"]["port"] = 10101
            print("[cfg] shadowmount.port 9021 is elfldr, not ShadowMount - using 10101")
    except (TypeError, ValueError):
        pass
    return cfg


def _atomic_write_json(path, obj, indent=None):
    """Write JSON via a temp file + os.replace() so a crash mid-write can never leave a truncated/corrupt
    file (config, hash cache, installed state). Returns True on success. [audit L3]"""
    d = os.path.dirname(os.path.abspath(path)) or "."
    tmp = None
    try:
        fd, tmp = tempfile.mkstemp(prefix=".pms_tmp_", dir=d)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(obj, f, indent=indent)
        os.replace(tmp, path)
        return True
    except OSError as e:
        print("[atomic-write] %s: %s" % (path, e))
        if tmp and os.path.exists(tmp):
            try: os.remove(tmp)
            except OSError: pass
        return False


def save_config(cfg, force=False):
    """Persist the config. Returns True when something was written.

    Refuses while the on-disk config could not be parsed, because writing then means replacing a
    file we could not read with defaults we invented. `force=True` is the deliberate override.
    """
    if _CONFIG_UNREADABLE and not force:
        print("[cfg] NOT saving: %s could not be read this session (%s). "
              "Fix or delete it first — saving now would overwrite it with defaults."
              % (CONFIG_PATH, _CONFIG_UNREADABLE))
        return False
    clean = {k: v for k, v in cfg.items() if not k.startswith("_")}
    return bool(_atomic_write_json(CONFIG_PATH, clean, indent=2))


def _port_open(host, port, timeout=0.4):
    """Is a TCP port accepting right now? Cheap and non-blocking-ish.

    A refused connect returns immediately; only a filtered or black-holed port costs the full timeout.
    Used to avoid paying an HTTP timeout to talk to a service that plainly is not there.

    NEVER POINT THIS AT A PAYLOAD LOADER. Opening GoldHEN's port 9090 and closing it again stops it
    listening - this project has lost the loader that way twice, once from nothing more than a port
    scan. The only thing that may touch that port is a POST carrying the whole payload, which is
    what ps4_start_shop() does.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((host, int(port)))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def lan_ip():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def local_ips():
    """Every non-loopback IPv4 this PC has — one per interface (Wi-Fi + Ethernet, etc.). [B11]"""
    ips = set()
    try:
        for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET):
            ip = info[4][0]
            if not ip.startswith("127."):
                ips.add(ip)
    except OSError:
        pass
    li = lan_ip()
    if li and not li.startswith("127."):
        ips.add(li)
    return sorted(ips)


def companion_ip_for(target_ip):
    """Pick the local IP on the same /24 as the console so a dual-homed PC (Wi-Fi + Ethernet) hands the
    console a pull URL it can actually reach. Falls back to lan_ip() when nothing matches — so single-NIC
    behavior is byte-identical to before. [B11]"""
    if not target_ip:
        return lan_ip()
    tp = target_ip.rsplit(".", 1)[0]
    for ip in local_ips():
        if ip.rsplit(".", 1)[0] == tp:
            return ip
    return lan_ip()


def assemble_sources(cfg):
    implicit = {"name": "companion-lan", "type": "lan",
                "base_url": "http://%s:%d/library/" % (lan_ip(), cfg["companion"]["port"]),
                "priority": 10, "companion": True}
    return [implicit] + list(cfg.get("sources", []))


# PS5 install destinations the picker can offer (real per-device placement works today for the MOUNT lane;
# DPI destination targeting stays gated behind dpi.device_key). [B6]
PS5_DEVICES = [
    {"id": "internal", "label": "Internal SSD"},
    {"id": "ext0", "label": "Extended (ext0)"},
    {"id": "ext1", "label": "Extended (ext1)"},
] + [{"id": "usb%d" % i, "label": "USB%d" % i} for i in range(8)]


def enumerate_pc_drives():
    """PC-side storage roots with free/total, so the UI can offer a place to build the Games tree. [B6]"""
    import shutil
    out = []
    if sys.platform == "win32":
        import string
        for letter in string.ascii_uppercase:
            root = "%s:\\" % letter
            if os.path.isdir(root):
                try:
                    du = shutil.disk_usage(root)
                    out.append({"id": "%s:" % letter, "label": "Drive %s:" % letter, "path": root,
                                "kind": "pc", "free": du.free, "total": du.total})
                except OSError:
                    pass
    else:
        for root in ("/", os.path.expanduser("~")):
            try:
                du = shutil.disk_usage(root)
                out.append({"id": root, "label": root, "path": root, "kind": "pc",
                            "free": du.free, "total": du.total})
            except OSError:
                pass
    return out


def drive_id_for_label(label):
    """Map a console-reported storage label back to the drive id the queue uses."""
    l = (label or "").strip().lower()
    if "internal" in l:
        return "internal"
    m = re.search(r"(usb\d)", l)
    if m:
        return m.group(1)
    if "extended" in l or "ext" in l:
        return "ext1"
    return l or "internal"


def mount_dest_for_drive(cfg, drive):
    """Where a PS5 backup must land for ShadowMount to pick it up, for the chosen drive.

    Verified over FTP on-device: backups live in /mnt/<drive>/homebrew (e.g. /mnt/ext1/homebrew,
    /mnt/usb0/homebrew ... /mnt/usb7/homebrew). ShadowMount scans those folders itself and mounts
    the title once the file is fully written, so our job is only to put it in the right place.
    'internal' has no /mnt entry and uses ShadowMount's own internal scan path."""
    sm = cfg.get("shadowmount", {})
    d = (drive or "").strip().lower()
    explicit = (sm.get("scan_paths_by_drive") or {}).get(d)
    if explicit:
        return explicit
    if d in ("ext0", "ext1", "ext2") or re.match(r"^usb[0-7]$", d):
        return "/mnt/%s/homebrew" % d
    if d in ("", "internal", "shadowmount", "auto"):
        return sm.get("scan_path", "/data/homebrew")
    return "/mnt/%s/homebrew" % d          # any future drive id follows the same shape


def ensure_library_tree(root, layout=LIBRARY_LAYOUT):
    """Create <root>/PS4 and <root>/PS5 so there is one obvious place to drop games.
    PS4 and PS5 live DIRECTLY under the root (no extra Games/ level). The scan walks every
    subfolder, so anything dropped anywhere beneath the root is found automatically."""
    created = []
    base = root
    for p in [base] + [os.path.join(base, s) for s in layout]:
        try:
            if not os.path.isdir(p):
                os.makedirs(p)
                created.append(p)
        except OSError as e:
            return {"ok": False, "error": str(e), "created": created, "root": base}
    return {"ok": True, "root": base, "created": created}


def ensure_all_folders(cfg):
    """Make every folder this app needs, on every start. Returns the list of what it had to create.

    THE APP KNOWS THE PATH AND THE LAYOUT, SO IT SHOULD NOT BE ASKING. Two places used to tell the
    owner a folder was missing and leave them to it: the games side had a "Create folders" button in
    Settings, and the payloads panel simply printed "That folder is not on this PC: <path>" across
    the top. Both of those are the app describing a job only it was in a position to do. The button
    stays, because pressing it on a specific root is still a sensible thing to want; it is just no
    longer the only way the folders come to exist.

    THE TWO TREES ARE DELIBERATELY SEPARATE AND MUST STAY SEPARATE. The payloads root is NOT added
    to library.local_paths - payloads.py states the reason in its own header and it is not a style
    preference: Library.scan() registers every .pkg it finds as a game, and normalise_pkg_names()
    RENAMES files on disk whose stem has characters outside [A-Za-z0-9._-]. Pointing the library at
    the homebrew folder would rewrite the owner's filenames and put five homebrews on their game
    shelf. So this creates both trees and crosses neither.

    Every failure is swallowed per-path: a second PC with a drive that is not plugged in must still
    boot, and a folder that cannot be made is not a reason for the app not to start.
    """
    made = []
    for p in (cfg.get("library", {}).get("local_paths") or []):
        try:
            r = ensure_library_tree(p)
            made.extend(r.get("created") or [])
        except Exception as e:
            print("[folders] %s: %s" % (p, e))
    try:
        r = payload_engine.ensure_source_tree(cfg)
        made.extend(r.get("created") or [])
        if not r.get("ok"):
            print("[folders] payloads root: %s" % r.get("error"))
    except Exception as e:
        print("[folders] payloads root: %s" % e)
    for p in made:
        print("[folders] created %s" % p)
    return made


def console_probe(ip, timeout=1.5):
    """What the console at this address says about itself, or None if that is not a console.

    Asks /api/health and requires on_console, rather than just opening a socket. Another PC on this
    network runs the companion on the same port, and "something answered 8710" would have this
    follow a console to a laptop - which this project has already done once, saving a peer PC as
    ps5_ip. The reply carries the platform and, since 3.67.0, the console's durable id."""
    if not ip:
        return None
    try:
        with urllib.request.urlopen("http://%s:8710/api/health" % ip, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
        return d if d.get("on_console") else None
    except Exception:
        return None


def _plat_of(d):
    """ps4 or ps5, from a health reply or a config entry. Unknown is a PS5 - the rule this whole
    app follows, because that is what a console that predates PS4 support is."""
    return "ps4" if str((d or {}).get("platform") or "").lower() == "ps4" else "ps5"


def _helper_arm_state(b, tid):
    """{helper_autoarm, helper_armed} for one title, or {} when the console cannot say.

    Absent keys mean "unknown", and the page treats unknown exactly as it behaved before these
    existed - so an older console payload is never worse off."""
    try:
        d = b.engine_agent() or {}
    except Exception:
        return {}
    if not d or d.get("ok") is False:
        return {}
    armed_for = str(d.get("armed_for") or "")
    armed = bool(d.get("armed_default")) or (tid in [x for x in armed_for.split(",") if x])
    return {"helper_autoarm": bool(d.get("autoarm")), "helper_armed": armed}


# What each console's content looked like the last time we asked, by console id (or address, for an
# entry too old to have one). Module level rather than per-Bridge: bridges are rebuilt whenever the
# fleet reloads, and a signature that reset with them would report a change that did not happen.
_CONTENT_SIG = {}


def note_content_change(fleet, health, con, log=None):
    """Did this console's installed content move since the last poll? If so, make the page re-read.

    apps_sig is a stat of app.db and addcont.db, answered by both payloads on /api/health. An older
    payload sends nothing, and nothing means unknown - in which case this does nothing at all, which
    is exactly how the app behaved before the field existed.

    Returns True when it bumped. Never raises: this runs inside the console tracker's loop, and a
    failure here must not stop the tracker from doing its own job.
    """
    try:
        sig = str((health or {}).get("apps_sig") or "")
        if not sig:
            return False
        # BOTH KEYS, because the id arrives LATER than the address. track_consoles learns a console's
        # id the first time it reports one, so the key moved from "10.0.0.86" to "d125ea52..." on that
        # pass - the new key had no previous value, and the first-sighting rule swallowed the first
        # real change after it. Either key matching counts as "seen before".
        cid = str((con or {}).get("console_id") or "")
        ip = str((con or {}).get("ip") or "")
        key = cid or ip
        if not key:
            return False
        prev = _CONTENT_SIG.get(cid) if cid else None
        if prev is None and ip:
            prev = _CONTENT_SIG.get(ip)
        _CONTENT_SIG[key] = sig
        if cid and ip and ip in _CONTENT_SIG:
            _CONTENT_SIG.pop(ip, None)      # the id is the durable name; do not keep two forever
        if prev is None or prev == sig:
            return False           # a first sighting is not news, and neither is no change
        ip = str((con or {}).get("ip") or "")
        for b in list(getattr(fleet, "bridges", {}).values()):
            if getattr(b, "ip", None) == ip:
                b.invalidate_apps()
                break
        lib = getattr(fleet, "library", None)
        if lib is not None:
            # ONE MESSAGE PER THING THAT HAPPENED. _finished_installing already bumps library_gen the
            # moment one of our own installs lands, and the page toasts on every bump - so without
            # this, forty-five seconds later the tracker sees apps_sig move and toasts a second time
            # for the same install. The owner has said twice that one is enough. The signature is
            # still recorded above, so the next genuine change is not missed.
            # SUPPRESS ONLY THE ECHO OF OUR OWN INSTALL. _finished_installing stamps _told_ms when
            # one of our tasks lands and bumps the gen itself; forty-five seconds later apps_sig
            # catches up and this would toast the same install a second time.
            # It deliberately does NOT stamp on its own bumps: two console-side changes a minute apart
            # are two things that happened, and the owner should see both.
            told = getattr(lib, "_told_ms", 0.0)
            if time.time() - told < 120.0:
                if log:
                    log(" %s content changed, and the library was told about that install already"
                        % (con.get("name") or con.get("platform") or ip or "console"))
                return False
            lib.gen += 1
        else:
            # NOTHING TO ACT ON. The signature is recorded, so the next change is still noticed - but
            # saying "the library will re-read" when there is no library to tell is a claim about
            # something that did not happen. Found by a test that could not previously get this far.
            return False
        if log:
            log(" %s changed what it has installed - the library will re-read"
                % (con.get("name") or con.get("platform") or ip or "console"))
        return True
    except Exception:
        return False


def track_consoles(cfg, fleet, log=None, scan=True):
    """Make the saved console addresses match reality. Returns what changed, as sentences.

    THE ONLY WRITER OF A CONSOLE'S ADDRESS. Everything else in this file reads consoles; this is
    where they are corrected, so there is one place to look when one is wrong.
    """
    say = log or (lambda m: None)
    conf = cfg.get("consoles") or []
    changed = []          # things worth TELLING somebody about
    dirty = [False]       # ...and things merely worth SAVING, which is not the same set

    # ---- 1. Who is where we think they are? One health call each; this is the cheap half.
    here, silent = {}, []
    for i, c in enumerate(conf):
        h = console_probe(c.get("ip"))
        if h:
            here[i] = h
            # CONTENT, not identity: apps_sig moving means something was installed, updated or
            # deleted on that console. Free - this health document is already in hand.
            note_content_change(fleet, h, c, log=say)
            # LEARN THE ID the first time a console reports one. An entry written before identity
            # existed, or by hand, has none - and it needs one before it can ever be followed.
            cid = h.get("console_id")
            if cid and c.get("console_id") != cid:
                # LEARNING AN ID IS NOT NEWS, BUT IT MUST PERSIST. This used to set the field and
                # nothing else, and because it added no line to `changed` the config was never
                # written - so every restart re-learned it and the id was only ever in memory. The
                # whole point of an id is that it outlives a restart, which made that a quiet
                # defeat of the feature rather than a cosmetic miss.
                dirty[0] = True
                if c.get("console_id"):
                    # The id at this address is not the one that used to be here: two consoles
                    # swapped leases, or this entry was pointed somewhere new by hand. Trust what
                    # is answering now - it is the only thing here that is measured.
                    changed.append("%s at %s is a different console than before"
                                   % (c.get("name") or _plat_of(c).upper(), c["ip"]))
                c["console_id"] = cid
            # AND THE FIRMWARE, WHICH IS WORTH AN ANNOUNCEMENT OF ITS OWN. Our payload can only run
            # on a firmware its SDK has kernel offsets for; past that it cannot establish kernel
            # read/write and dies before its first log line, so the console simply stops having a
            # shop and nothing anywhere says why. Measured 2026-10-01: the owner updated a PS5 from
            # 12.70 to 13.60, the build knew up to 13.40, and the symptom was Payload Manager
            # reporting a clean "Sent 45367592 bytes to loader" over and over while nothing started.
            # It took an hour of elimination to reach a number the console could have volunteered.
            #
            # So the number is kept beside the id, and a CHANGE is news rather than a quiet field
            # update - that line is the one that would have answered it immediately.
            fwv = str(h.get("fw") or "").strip()
            if fwv and fwv != "unknown" and c.get("fw") != fwv:
                dirty[0] = True
                if c.get("fw"):
                    changed.append("%s is on firmware %s now - it was %s. If its payloads stop "
                                   "starting, the build needs a newer SDK."
                                   % (c.get("name") or _plat_of(c).upper(), fwv, c["fw"]))
                c["fw"] = fwv
        else:
            silent.append(i)

    if not silent or not scan:
        # Nothing to look for - but an id learned above still has to reach the disk.
        if dirty[0]:
            try:
                if fleet is not None:
                    fleet.reload()
            except Exception:
                pass
            try:
                save_config(cfg)
            except Exception:
                pass
        return changed

    # ---- 2. Something is missing, so look. This is the expensive half and it is why the caller
    # rate-limits us.
    seen = []
    for f in discover_ps5(cfg):
        if not f.get("confirmed"):
            continue
        h = console_probe(f["ip"])
        if h:
            seen.append((f["ip"], h))
    if not seen:
        return changed

    taken = {c.get("ip") for i, c in enumerate(conf) if i in here}
    # Every id this PC already accounts for, so adoption below never adds a console twice.
    known_ids = {c.get("console_id") for c in conf if c.get("console_id")}

    for i in silent:
        c = conf[i]
        want_id = c.get("console_id")
        want_plat = _plat_of(c)
        old_ip = c.get("ip")

        # IDENTITY FIRST, and it is certainty rather than inference.
        hit = [ip for ip, h in seen
               if want_id and h.get("console_id") == want_id and ip not in taken]
        how = "id matches"

        # A SAVED ID THAT NOTHING ON THE NETWORK REPORTS IDENTIFIES NOTHING, which makes it exactly
        # as useless as having none - and it fails the same way, permanently. Measured here: the
        # owner's PS4 moved to another address AND regenerated its id (the file behind it is written
        # once and had been re-created), so the saved pair matched nothing, the id-first lookup found
        # no candidate, and this fallback was skipped because `want_id` was merely PRESENT. The
        # companion went on asking a dead address for days while the console sat answering on
        # another one, which also made every PS4 tile in the payload panel read as unreachable.
        #
        # The protection this guard really provides is a house with a SECOND console of the same
        # platform, and that is untouched: it still fires only when EXACTLY ONE console of this
        # platform is unaccounted for, and the move is announced.
        seen_ids = {h.get("console_id") for _ip, h in seen if h.get("console_id")}
        if not hit and (not want_id or want_id not in seen_ids):
            # THE ENTRY HAS NOTHING USABLE TO IDENTIFY IT BY - and that is not a rare corner. An id
            # is only
            # ever learned from a console you can already reach, so an entry whose address went stale
            # BEFORE ids existed can never learn one. Chicken and egg, and it is permanent.
            #
            # THIS USED TO REQUIRE THE CANDIDATE TO HAVE NO ID EITHER, and that quietly disabled the
            # whole fallback the moment every console started reporting one. Measured on the owner's
            # second PC: its PS4 entry had no id, the real PS4 answered at another address WITH an id,
            # and the entry could be neither followed (no id to match) nor adopted (the platform
            # already had an entry). A dead address for ever, on a PC where everything else worked.
            #
            # The old reasoning was that a candidate we CAN name must be a DIFFERENT console from an
            # entry we cannot. That does not follow: the entry has no id to compare against, so the
            # candidate's id says nothing about the entry - only that the candidate runs a build new
            # enough to have one, which is now all of them.
            #
            # What the guard is really for is a house with a SECOND console of the same platform, and
            # that is still refused: this fires only when EXACTLY ONE console of this platform is
            # unaccounted for, never when its id already belongs to another entry, and never across
            # platforms. The move is announced, so a wrong guess is visible and correctable instead of
            # silent.
            cands = [ip for ip, h in seen
                     if _plat_of(h) == want_plat
                     and ip not in taken
                     and (not h.get("console_id") or h["console_id"] not in known_ids)]
            if len(cands) == 1:
                hit = cands
                how = ("the only %s on this network that no other entry accounts for"
                       % want_plat.upper())
            elif len(cands) > 1:
                say("  %s could be any of %d consoles - leaving its address alone"
                    % (c.get("name") or want_plat.upper(), len(cands)))

        if not hit:
            continue

        new_ip = hit[0]
        c["ip"] = new_ip
        taken.add(new_ip)
        nh = dict(seen)[new_ip]
        if nh.get("console_id"):
            c["console_id"] = nh["console_id"]
            known_ids.add(nh["console_id"])
        # The two legacy address keys still drive a great deal and must follow the list.
        if want_plat == "ps4" and cfg.get("ps4_ip") == old_ip:
            cfg["ps4_ip"] = new_ip
        elif want_plat != "ps4" and cfg.get("ps5_ip") == old_ip:
            cfg["ps5_ip"] = new_ip
        changed.append("%s moved %s -> %s (%s)"
                       % (c.get("name") or want_plat.upper(), old_ip, new_ip, how))

    # ---- 3. A console we have never met, answering right now. The narrow adoption rule: a
    # platform with no entry at all. An id we do not know is not by itself a reason to add one -
    # a console the owner deliberately removed would come straight back.
    have_plat = {_plat_of(c) for c in conf}
    for ip, h in seen:
        p = _plat_of(h)
        if p in have_plat or ip in taken:
            continue
        if h.get("console_id") and h["console_id"] in known_ids:
            continue            # this is a console we already have, currently at another address
        used = {str(x.get("id") or "") for x in conf}
        cid, n = p, 0
        while cid in used:
            n += 1
            cid = "%s-%d" % (p, n)
        conf.append({"id": cid, "name": p.upper(), "ip": ip, "platform": p,
                     "console_id": h.get("console_id") or "",
                     "ftp_port": cfg.get("ftp", {}).get("port", 2121)})
        cfg["consoles"] = conf
        have_plat.add(p)
        taken.add(ip)
        if p == "ps4" and not cfg.get("ps4_ip"):
            cfg["ps4_ip"] = ip
        elif p != "ps4" and not cfg.get("ps5_ip"):
            cfg["ps5_ip"] = ip
        changed.append("found a %s at %s that this PC had never seen" % (p.upper(), ip))

    if changed or dirty[0]:
        # NOT reconcile_consoles(). It folds the settings panel's two address FIELDS into the
        # console list, and from a list this function has just corrected it does the opposite of
        # what is wanted: a test with no ps4_ip/ps5_ip set watched it empty the list completely
        # after a correct move was applied. The legacy keys are kept in step above, one at a time,
        # only when they actually pointed at the address that changed.
        #
        # RELOAD, NEVER REPLACE. The queue holds a reference to this exact Fleet object, so
        # swapping in a new one would leave every running job talking to the old addresses.
        # Ps5Bridge reads its address out of the config dict on each call, so a reload is all it
        # takes for everything to follow.
        try:
            if fleet is not None:
                fleet.reload()
        except Exception:
            pass
        try:
            save_config(cfg)
        except Exception:
            pass
        for m in changed:
            say(" %s" % m)
    return changed




def start_console_tracker(httpd):
    """Keep the saved addresses true, for as long as the app is running.

    The cheap check runs often; the /24 scan only after a console has actually gone quiet, and no
    more than once every few minutes. A console that is simply switched off must not have this PC
    sweeping the network all day - the scan costs 254 connects per port.
    """
    def loop():
        time.sleep(40)                       # let startup settle; it does its own first pass
        last_scan = 0.0
        last_look = 0.0                      # the "never met this platform" sweep, separately
        look_every = 900.0                   # 15 min to start, doubling while it finds nothing
        while True:
            try:
                cfg = httpd.cfg
                quiet = [c for c in (cfg.get("consoles") or [])
                         if not console_probe(c.get("ip"), timeout=1.2)]
                now = time.time()
                # Scan when something we KNOW about is missing, and not more than every few minutes.
                may_scan = bool(quiet) and (now - last_scan) > 180

                # AND, MUCH MORE SLOWLY, when a whole platform has never been met. The rule above is
                # right for a console that went quiet and wrong for one that was never configured:
                # on a PC whose only entry is a PS5 that answers, `quiet` is empty for ever, so the
                # sweep never ran and a PS4 on the same network could never be adopted however long
                # the app stayed open. That was the second half of a real two-PC report.
                # It backs off while it keeps finding nothing, so a household with no PS4 does not
                # pay 254 connects every quarter of an hour indefinitely, and it stops entirely once
                # a console of that platform is adopted - the platform is no longer missing.
                have_plats = {(_plat_of(c) or "") for c in (cfg.get("consoles") or [])}
                missing = [p for p in ("ps5", "ps4") if p not in have_plats]
                if not may_scan and missing and (now - last_look) > look_every:
                    may_scan = True
                    last_look = now
                    look_every = min(look_every * 2, 3600.0)
                elif not missing:
                    look_every = 900.0       # met one: be quick again if another goes missing later
                if may_scan:
                    last_scan = time.time()
                track_consoles(cfg, getattr(httpd, "fleet", None),
                               log=lambda m: print("[consoles]%s" % m), scan=may_scan)
            except Exception as e:
                print("[consoles] check skipped: %r" % e)
            time.sleep(45)

    threading.Thread(target=loop, daemon=True).start()


def ps4_start_shop(ip, log=None):
    """Hand the PS4 payload to GoldHEN's loader. Returns a sentence, or None when not attempted."""
    say = log or (lambda m: None)
    if not os.path.exists(PS4_PAYLOAD_ELF):
        return None                      # a build that does not carry it simply cannot do this
    try:
        body = io.open(PS4_PAYLOAD_ELF, "rb").read()
    except Exception:
        return None
    req = urllib.request.Request("http://%s:9090/" % ip, data=body,
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            r.read()
    except Exception as e:
        say("  %s: the payload loader did not take it (%s)" % (ip, e.__class__.__name__))
        return None
    # It is only started when the shop answers. "The loader took the bytes" is not the same thing,
    # and reporting it as success is how a hand-over that went nowhere reads as working.
    end = time.time() + 40
    while time.time() < end:
        time.sleep(2)
        h = console_probe(ip, timeout=2.0)
        if h:
            return "started the shop on %s (v%s)" % (ip, h.get("version") or "?")
    return "handed the payload to %s but its shop did not come up" % ip


def start_console_autostart(httpd):
    """Bring up the shop on any console that is switched on and does not have one.

    See the long note in tools for why this exists: a PS4 payload does not survive a reboot and
    GoldHEN has no autoload, so after every restart something has to hand the ELF over. The icon
    does it when pressed; this does it when nobody presses anything.
    """
    def loop():
        time.sleep(60)                     # after the tracker's first pass, so addresses are true
        tried = {}
        misses = {}
        while True:
            try:
                for c in (httpd.cfg.get("consoles") or []):
                    ip = c.get("ip")
                    if not ip or _plat_of(c) != "ps4":
                        continue           # the PS5 has its own lane; see the note above
                    if console_probe(ip, timeout=1.2):
                        misses[ip] = 0
                        continue           # its shop is already answering
                    # A SINGLE MISS IS NOT A DEAD CONSOLE - IT IS USUALLY US.
                    #
                    # Reloading the payload deliberately (tools/push_consoles.py) takes the shop
                    # down for a few seconds. This loop used to see that one gap and hand the
                    # console the ELF BUNDLED IN THIS EXE, which is whatever was current when the
                    # exe was built - so a freshly pushed payload got silently replaced by an older
                    # one, and the next test measured the wrong build. That cost a real debugging
                    # session: the console reported a build eight hours older than the one just
                    # pushed, and its boot undid the very thing being tested.
                    #
                    # Two consecutive misses ~30 s apart means the shop is actually gone, not
                    # restarting. A genuinely off-and-on console still recovers, one cycle later.
                    misses[ip] = misses.get(ip, 0) + 1
                    if misses[ip] < 2:
                        continue
                    # Is the console even there? FTP survives a rest/resume; the loader does not,
                    # so this is the state worth acting on rather than a console that is off.
                    if not (_port_open(ip, c.get("ftp_port") or 2121) or _port_open(ip, 1337)):
                        continue
                    if time.time() - tried.get(ip, 0) < 300:
                        continue
                    tried[ip] = time.time()
                    print("[autostart] %s is on with no shop - handing it the payload" % ip)
                    msg = ps4_start_shop(ip, log=lambda m: print("[autostart]%s" % m))
                    if msg:
                        print("[autostart] %s" % msg)
            except Exception as e:
                print("[autostart] skipped: %r" % e)
            time.sleep(30)

    threading.Thread(target=loop, daemon=True).start()


def consoles_from_cfg(cfg):
    raw = list(cfg.get("consoles") or [])
    if not raw and cfg.get("ps5_ip"):
        raw = [{"name": "PS5", "ip": cfg["ps5_ip"]}]
    out = []
    for i, c in enumerate(raw):
        if not c.get("ip"):
            continue
        # PLATFORM IS CARRIED THROUGH, NOT GUESSED HERE. A config written before PS4 support
        # existed says nothing about the platform, and reading that silence as "PS5" is exactly how
        # a PS4 would be handed the PS5 install lane. So an unknown platform stays empty and the
        # bridge learns it from the console's own /api/health, which is the only honest source.
        plat = str(c.get("platform") or "").strip().lower()
        if plat not in ("ps4", "ps5"):
            plat = ""
        # The default id keeps its historical "ps5-N" spelling for anything that is not known to be
        # a PS4: ids are saved in config.json and referenced by the UI and the queue, so renaming
        # them for existing setups would orphan every stored selection.
        out.append({"id": c.get("id") or ("%s-%d" % ("ps4" if plat == "ps4" else "ps5", i)),
                    "name": c.get("name") or c["ip"],
                    "ip": c["ip"], "platform": plat,
                    # THE FIRMWARE TRAVELS WITH THE ENTRY. track_consoles learns it from the
                    # console's own health and saves it; dropping it here meant the number was in
                    # config.json and nowhere a person could see it. It is the one fact that says
                    # whether a build can run on that console at all - see the note in
                    # track_consoles - so it belongs in the console list the UI reads.
                    "fw": str(c.get("fw") or ""),
                    "ftp_port": c.get("ftp_port", cfg["ftp"].get("port", 2121))})
    return out


# What the PS4's background transfer service calls each kind of package. These are the same two
# letters the package's own param.sfo CATEGORY carries (gd = game, gp = game patch, ac = additional
# content), so this is the file's own description of itself rather than a label of ours - see
# pkg_meta.kind_from_category, which reads it in the other direction.
PS4_PACKAGE_TYPE = {"base": "PS4GD", "backport": "PS4GD",
                    "update": "PS4GP", "patch": "PS4GP",
                    "dlc": "PS4AC"}


def http_get_json(url, timeout=3):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None


def registry_key(registry, raw):
    """Canonicalize a path segment to the registry key. Registry keys are quote(basename);
    a request may arrive quoted (%5B) or unquoted ([). Try both so keys always match."""
    if raw in registry:
        return raw
    q = quote(raw)
    if q in registry:
        return q
    return None


def pkg_facts_for_key(library, key):
    """Content id, byte size and kind of one library item, found by its install key.

    The PS4 install lane needs all three before the console will accept the task (see
    Ps5Bridge._pkg_query), and a caller may only have been handed the key. Every value here was read
    out of the package's own param.sfo when the library was scanned, so this is a lookup, not a
    second parse. Returns a dict with empty/zero fields when the key is unknown - never None, so no
    caller has to guard it, and a missing content id simply means the console is asked the way the
    PS5 has always been asked.
    """
    out = {"content_id": "", "size": 0, "kind": ""}
    if not key or library is None:
        return out
    rk = registry_key(getattr(library, "file_registry", {}) or {}, key) or key
    try:
        out["size"] = int((getattr(library, "file_sizes", {}) or {}).get(rk) or 0)
    except (TypeError, ValueError):
        out["size"] = 0
    for g in getattr(library, "games", []) or []:
        for bucket in ("base", "updates", "dlc"):
            for it in (g.get(bucket) or []):
                if it.get("install_key") not in (key, rk):
                    continue
                out["content_id"] = str(it.get("content_id") or "")
                out["kind"] = str(it.get("kind") or "")
                if not out["size"]:
                    try:
                        out["size"] = int(it.get("size") or 0)
                    except (TypeError, ValueError):
                        pass
                return out
    return out


def now_ms():
    return int(time.time() * 1000)


# --------------------------------------------------------------------------- #
# library                                                                      #
# --------------------------------------------------------------------------- #
GAME_FOLDER_MARKERS = ("eboot.bin", "sce_sys", "param.json", "param.sfo", "sce_module")


def is_game_folder(path, name):
    """Is this directory a game stored unpacked, rather than an ordinary folder?

    ShadowMount runs a folder the same way it runs a container, so one belongs in the library like
    any other backup. Recognised by the structure a real dump has — never by name alone, so the
    PS4/PS5 layout folders and stray directories are left alone.
    """
    # A marker directory is part of a game, never a game itself. The real scan prunes the parent
    # before reaching it, but saying so here means the test does not depend on that ordering.
    if str(name or "").lower() in GAME_FOLDER_MARKERS:
        return False
    try:
        entries = set(e.lower() for e in os.listdir(path))
    except OSError:
        return False
    if any(m in entries for m in GAME_FOLDER_MARKERS):
        return True
    # A folder named for a title id that holds the usual dump layout counts too.
    if re.search(r"(CUSA|PPSA)\d{5}", name or "", re.I):
        return any(m in entries for m in GAME_FOLDER_MARKERS)
    return False


def is_backup_item(item, game=None):
    """Is this file a ShadowMount container rather than a PKG?

    Checked three ways because a peer on an older build may state none of them: the kind it was
    filed under, the format either side reported, and finally the filename — which is proof on its
    own. Every container in MOUNT_EXTS counts, so .ffpfs / .exfat / .ffpkg / .iso behave exactly as
    .ffpfsc does rather than only the one format we happened to test with.
    """
    item = item or {}
    k = str(item.get("kind") or "").lower()
    if k == "backup":
        return True
    # An ADD-ON is a PKG. Always. It is never a ShadowMount container, whatever the game it
    # belongs to happens to be stored as.
    #
    # Without this line the checks below consult the GAME's format as a fallback - which is right
    # for the base item and badly wrong for everything else. A PS5 game arrives as a .ffpfsc
    # backup, so once its updates and DLC were correctly grouped onto that game's card, every one
    # of them inherited "ffpfsc" from the parent and was declared a backup. They were then routed
    # down the mount/delegate lane as kind "backup", which cost them their real kind: the install
    # watcher saw kind "base", waited for a full-size app.pkg that a 1 MB DLC can never produce,
    # and after four minutes reported "Console rejected this package - delivered in full, but it
    # won't install (bad dump)". Measured on Borderlands 4's Firehawk's Finery: bgft.db said
    # status 1026, error 0x00000000, and addcont.db held the registration. The install had
    # succeeded completely; only the report was wrong.
    if k in ADDON_KINDS:
        return False
    for f in (item.get("format"), (game or {}).get("format")):
        if f and ("." + str(f).lower().lstrip(".")) in MOUNT_EXTS:
            return True
    for n in (item.get("file"), (game or {}).get("name")):
        if fmt_from_filename(n):
            return True
    return False


def fmt_from_filename(fn):
    """Container type from a filename, or None if it is not a backup container.

    The extension is proof on its own, which is what makes a backup identifiable even when it came
    from a peer that never told us what it was."""
    ext = os.path.splitext(str(fn or ""))[1].lower()
    return ext.lstrip(".") if ext in MOUNT_EXTS else None


def mount_name(fn):
    """Readable title for a backup container, whose only metadata is its filename.

    A PKG carries a param.sfo with the real title, but a ShadowMount backup does not — so the name
    on the card is whatever the file is called. Left alone that reads "Elden Ring - PPSA04610-app",
    with the id and the container's role spelled out twice. Strip the parts that are already shown
    in their own fields (platform tag, title id, version tag, -app/-patch suffix) and keep the name.
    """
    stem = os.path.splitext(fn)[0]
    s = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)              # [PS5], (v1.02), [DLPSGAME.COM]
    s = re.sub(r"[_]+", " ", s)
    s = re.sub(r"\b(CUSA|PPSA)\d{5}\b", " ", s, flags=re.I)     # the id has its own field
    # Hyphen-attached and at the end only. These markers are written "PPSA04610-app" by the backup
    # tools; matching a bare trailing word instead turned "The Last Game" into "The Last".
    s = re.sub(r"-\s*(app|patch|game|dlc|data)\s*$", "", s, flags=re.I)
    s = " ".join(s.split()).strip(" -._")
    if not s:                                                    # nothing but tags: fall back
        s = " ".join(re.sub(r"[\[\]\(\)_]+", " ", stem).split()).strip(" -.")
    return s or fn


PART_PATTERNS = [
    re.compile(r"[\s._\-\[(]part[\s._\-]*(\d{1,2})\b", re.I),
    re.compile(r"[\s._\-\[(]pt[\s._\-]*(\d{1,2})\b", re.I),
    re.compile(r"[\s._\-\[(](\d{1,2})\s*of\s*(\d{1,2})[\s._\-\])]", re.I),
    re.compile(r"\.part(\d{1,2})\b", re.I),
    re.compile(r"[\s._\-\[(]disc[\s._\-]*(\d{1,2})\b", re.I),
]


def part_info(filename):
    """(part_number, total_or_None, stem_without_the_part_marker) for a split release.

    Returns (None, None, stem) when the name carries no part marker. The stem is what
    groups the pieces together, so "Game Part 1.pkg" and "Game Part 2.pkg" collapse into
    one item while two unrelated packages never do."""
    stem = os.path.splitext(filename)[0]
    for rx in PART_PATTERNS:
        m = rx.search(stem)
        if not m:
            continue
        num = int(m.group(1))
        total = None
        if rx.groups >= 2 and m.lastindex and m.lastindex >= 2:
            try:
                total = int(m.group(2))
            except (TypeError, ValueError):
                total = None
        cleaned = (stem[:m.start()] + " " + stem[m.end():]).strip()
        cleaned = re.sub(r"[\s._\-]+", " ", cleaned).strip(" -._")
        return num, total, cleaned.lower()
    return None, None, re.sub(r"[\s._\-]+", " ", stem).strip(" -._").lower()


def group_parts(items):
    """Collapse split releases into single entries carrying an ordered `parts` list.

    Grouping is per (kind, title_id, stem) so a two-part BASE and a two-part UPDATE of the
    same game stay separate. Anything without a part marker is returned untouched."""
    out, groups = [], {}
    for it in items:
        num, total, stem = part_info(it.get("file") or "")
        if num is None:
            out.append(it)
            continue
        gk = (it.get("kind"), it.get("title_id"), stem)
        g = groups.get(gk)
        if not g:
            g = dict(it)
            g["parts"] = []
            g["multi_part"] = True
            g["parts_expected"] = total
            groups[gk] = g
            out.append(g)
        g["parts"].append({"install_key": it.get("install_key"), "file": it.get("file"),
                           "size": it.get("size", 0), "part": num})
        if total and not g.get("parts_expected"):
            g["parts_expected"] = total
    for g in groups.values():
        g["parts"].sort(key=lambda p: p["part"])
        g["size"] = sum(p.get("size") or 0 for p in g["parts"])
        # the first part is what a single-file install would have used
        g["install_key"] = g["parts"][0]["install_key"]
        g["parts_count"] = len(g["parts"])
        exp = g.get("parts_expected")
        g["parts_missing"] = (max(0, exp - len(g["parts"])) if exp else 0)
        nums = [p["part"] for p in g["parts"]]
        # a gap means a piece is simply not here; say so rather than installing a broken set
        g["parts_gap"] = bool(nums) and (sorted(nums) != list(range(min(nums), min(nums) + len(nums))))
    return out


def filename_fallback(fn):
    """Best-effort metadata when the package itself cannot be read.

    PS4 never really lands here: pkg_meta.parse_pkg() reads its param.sfo and returns a real
    CATEGORY (gd/gp/ac). PS5 ALWAYS lands here, because a PS5 package is a different container
    entirely - magic 7F 46 49 48 (FIH), not PS4's 7F 43 4E 54 (CNT) - and its param.sfo is not readable
    (verified: the SFO magic appears nowhere in the first 4 MB of four different PS5 packages).
    So for PPSA titles this
    function IS the classifier, and defaulting everything to "base" is what gave every PS5 update
    and DLC its own library card named after a content id.

    The filename still carries the truth, in two documented shapes:
        UP1001-PPSA01494_00-FIREHAWKSFINERY0.pkg      content id, title id inline
        UP4321-00-0014691966945618-PPSA07064.pkg      content id, title id appended
        Disney-Epic-Mickey-Rebrushed-v01-000-000-PPSA15656.pkg   an UPDATE (-v<maj>-<min>-<pt>)
    """
    stem = os.path.splitext(fn)[0]
    m = re.search(r"(CUSA\d{5}|PPSA\d{5})", stem, re.IGNORECASE)
    tid = m.group(1).upper() if m else None
    # Whole words only. A bare substring test read "Dispatch" as a patch and "Addlc..." as DLC;
    # release names separate their tags with - _ . or spaces, which \b treats as boundaries.
    kind = ("update" if re.search(r"\b(update|patch)\b", stem, re.I)
            else ("dlc" if re.search(r"\bdlc\b", stem, re.I) else "base"))
    # The content id, when the name is the canonical one (UP1001-PPSA01494_00-FIREHAWKSFINERY0).
    # It was hard-coded to "" for every file that lands here - every PS5 package - so
    # already_installed(kind="dlc") could never match addcont.db and an installed PS5 add-on was
    # never skipped.
    cm = re.search(r"\b([A-Z]{2}\d{4}-(?:CUSA|PPSA)\d{5}_00-[A-Z0-9_]{16})\b", stem, re.I)
    content_id = cm.group(1).upper() if cm else ""
    ver = None
    if tid and tid.startswith("PPSA"):
        # A PS5 game arrives as a ShadowMount backup (.ffpfsc), never as a .pkg, so a PS5 .pkg is
        # an add-on. Which kind is readable from the name: a version triple means an update,
        # anything else is downloadable content.
        vm = re.search(r"[-_]v(\d{1,2})[-.](\d{2,3})[-.](\d{2,3})", stem, re.IGNORECASE)
        if vm:
            kind, ver = "update", "%02d.%s" % (int(vm.group(1)), vm.group(2))
        elif kind == "base":
            kind = "dlc"
    name = re.sub(r"[\[\]\(\)_]+", " ",
                  re.sub(r"(CUSA\d{5}|PPSA\d{5}|\[BASE\]|\[UPDATE\]|\[DLC\])", "", stem, flags=re.I))
    return {"title_id": tid, "name": " ".join(name.split()).strip(" -.") or fn, "kind": kind,
            "version": ver, "region": (pkg_meta.region_from_content_id(content_id) if content_id else "—"),
            "content_id": content_id, "icon": False}


# --------------------------------------------------------------------------- #
# PKG filename normalisation                                                    #
# --------------------------------------------------------------------------- #
# Anything outside this set breaks the install lane. The console's DPI daemon is handed a URL to
# the file, and a name carrying spaces or brackets — "Cars 3 Driven to Win (CUSA07083) -
# [DLPSGAME.COM].pkg" — comes back as a bare "HTTP 500: Error" only AFTER the queue has accepted
# the job. Fixing the name on sight means a bad one never reaches the installer at all.
_PKG_UNSAFE = re.compile(r"[^A-Za-z0-9._-]")
_PKG_TERMINAL_STATES = ("playable", "error", "canceled", "submitted", "installed", "done")


def safe_pkg_filename(meta, fn):
    """The name a PKG should have: no spaces, no brackets, game name and title id preserved.

        Cars 3 Driven to Win (CUSA07083) - [DLPSGAME.COM].pkg
        -> Cars-3-Driven-to-Win-CUSA07083.pkg

    Built from the PKG's OWN param.sfo (title + title id + version) rather than from the old
    filename, so site stamps and release-group tags fall away and the game keeps its real name.
    Falls back to cleaning the filename when a PKG has no readable metadata.

    A base package gets `Name-TITLEID`. Updates and DLC additionally carry their kind and version,
    because they live in the same folder as the base and must not collapse onto its name — the
    library really does hold Castle Crashers at both v1.00 (227 MB base) and v1.04 (10 MB update).
    """
    stem = os.path.splitext(fn)[0]
    meta = meta or {}
    tid = (meta.get("title_id") or "").upper()
    if not tid:
        m = re.search(r"(CUSA\d{5}|PPSA\d{5})", stem, re.I)
        tid = m.group(1).upper() if m else ""

    name = (meta.get("title") or "").strip()
    if not name:
        # No param.sfo: clean the filename instead — drop bracketed junk and the id.
        name = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", stem)
        name = re.sub(r"(CUSA\d{5}|PPSA\d{5})", " ", name, flags=re.I)
        name = re.sub(r"[._]+", " ", name)
    name = _PKG_UNSAFE.sub("-", name)
    name = re.sub(r"-{2,}", "-", name).strip("-._") or "PKG"

    parts = [name]
    if tid:
        parts.append(tid)
    kind = (meta.get("kind") or "").lower()
    if kind and kind != "base":
        parts.append(kind.upper())
        ver = (meta.get("app_ver") or meta.get("version") or "").strip().lstrip("vV")
        if ver:
            parts.append("v" + _PKG_UNSAFE.sub("-", ver))
        if kind == "dlc":
            # Two DLC for one title would otherwise land on the same name.
            cid = (meta.get("content_id") or "").strip()
            tail = cid.rsplit("-", 1)[-1] if "-" in cid else ""
            if tail and tail.upper() != tid:
                parts.append(_PKG_UNSAFE.sub("-", tail))
    return "-".join(p for p in parts if p) + ".pkg"


class Library:
    def __init__(self, cfg):
        self.cfg = cfg
        self.queue = None       # set in main(); lets a rename skip a file a live job is using
        self.file_registry = {}
        self.file_sizes = {}
        self.games = []
        self.is_empty = True
        self.gen = 0            # bumps every scan; the UI polls it to auto-refresh [B4]
        self.last_scan_ms = 0

    def _register(self, abspath, reg=None, sizes=None):
        reg = self.file_registry if reg is None else reg
        sizes = self.file_sizes if sizes is None else sizes
        key = quote(os.path.basename(abspath))
        base, i = key, 1
        while key in reg and reg[key] != abspath:
            key, i = "%s.%d" % (base, i), i + 1
        reg[key] = abspath
        sizes[key] = os.path.getsize(abspath)
        return key

    def _register_dir(self, abspath, reg, sizes):
        """Same as _register for a game stored as a FOLDER, whose size is the sum of its contents.

        Kept separate because getsize() on a directory reports the entry, not the game — a folder
        registered through the file path would claim to be a few kilobytes."""
        key = quote(os.path.basename(abspath))
        base, i = key, 1
        while key in reg and reg[key] != abspath:
            key, i = "%s.%d" % (base, i), i + 1
        reg[key] = abspath
        total = 0
        for dp, _d, fs in os.walk(abspath):
            for f in fs:
                try:
                    total += os.path.getsize(os.path.join(dp, f))
                except OSError:
                    pass
        sizes[key] = total
        return key

    def _busy_paths(self):
        """Files a live queue job is reading right now — never rename one of those."""
        busy = set()
        q = self.queue
        if q is None:
            return busy
        try:
            for t in q.snapshot():
                if t.get("state") in _PKG_TERMINAL_STATES:
                    continue
                lp = t.get("local_path")
                if lp:
                    busy.add(os.path.normcase(os.path.abspath(lp)))
        except Exception:
            pass
        return busy

    def normalise_pkg_names(self):
        """Give every PKG in the library a name the installer can fetch.

        Runs at the top of every scan, and the folder watcher calls scan() whenever something
        appears — so a package dropped into the games folder is fixed before it is ever listed,
        let alone queued. Idempotent: a name that is already safe is left completely alone, which
        is also what stops the watcher (which fires on name changes) from looping.

        Deliberately narrow — it touches ONLY `.pkg` files under the configured library folders.
        PS5 backups, anything on the console, and every other system are untouched.
        """
        renamed = []
        busy = self._busy_paths()
        now = time.time()
        for root in self.cfg["library"].get("local_paths", []):
            if not root or not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                for fn in files:
                    if not fn.lower().endswith(".pkg"):
                        continue
                    if not _PKG_UNSAFE.search(os.path.splitext(fn)[0]):
                        continue                      # already safe — do not even open it
                    ap = os.path.join(dirpath, fn)
                    try:
                        st = os.stat(ap)
                    except OSError:
                        continue
                    # Still being written? Leave it; the next scan will pick it up.
                    if now - st.st_mtime < 15:
                        continue
                    if os.path.normcase(os.path.abspath(ap)) in busy:
                        continue
                    try:
                        meta = pkg_meta.parse_pkg(ap)
                    except Exception:
                        meta = None
                    new = safe_pkg_filename(meta, fn)
                    if new == fn:
                        continue
                    target = os.path.join(dirpath, new)
                    if os.path.exists(target):
                        # Never overwrite: a same-named neighbour is a different package.
                        stem, ext = os.path.splitext(new)
                        n = 2
                        while os.path.exists(os.path.join(dirpath, "%s-%d%s" % (stem, n, ext))) and n < 100:
                            n += 1
                        target = os.path.join(dirpath, "%s-%d%s" % (stem, n, ext))
                        if os.path.exists(target):
                            continue
                    try:
                        os.rename(ap, target)
                    except OSError as e:
                        # Locked by another process (a copy still finishing, an antivirus scan).
                        print("[pkgname] skipped %s: %s" % (fn, e))
                        continue
                    renamed.append((fn, os.path.basename(target)))
                    print("[pkgname] %s -> %s" % (fn, os.path.basename(target)))
        return renamed

    def scan(self):
        # A PKG whose name carries spaces or brackets fails the install with a bare "HTTP 500"
        # from the DPI daemon, so names are fixed BEFORE anything is registered — the rest of the
        # app then only ever sees a name that works.
        try:
            self.normalise_pkg_names()
        except Exception as e:
            print("[pkgname] pass skipped: %s" % e)
        # Build into local dicts and swap in at the very end, so a concurrent /library/ request during a
        # rescan never sees a half-built (or momentarily empty) registry and 404s. [B4 atomic scan swap]
        reg, sizes = {}, {}
        by_title, loose, mounts = {}, [], []
        n_parsed = n_fallback = 0
        for root in self.cfg["library"].get("local_paths", []):
            if not root or not os.path.isdir(root):
                continue
            for dirpath, _dirs, files in os.walk(root):
                # A game stored as a FOLDER instead of a container. ShadowMount mounts it the same
                # way, so it is listed the same way. Taken out of the walk once claimed, or its
                # internal files would come back a second time as loose packages.
                claimed = []
                for d in list(_dirs):
                    full = os.path.join(dirpath, d)
                    if not is_game_folder(full, d):
                        continue
                    fkey = self._register_dir(full, reg, sizes)
                    ftid = re.search(r"(CUSA\d{5}|PPSA\d{5})", d, re.I)
                    ftid = ftid.group(1).upper() if ftid else None
                    if re.search(r"\[\s*PS5\s*\]", d, re.I):   fplat = "PS5"
                    elif re.search(r"\[\s*PS4\s*\]", d, re.I): fplat = "PS4"
                    elif ftid:                                 fplat = "PS5" if ftid.startswith("PPSA") else "PS4"
                    else:                                       fplat = "BACKUP"
                    mounts.append({"install_key": fkey, "file": d, "size": sizes.get(fkey, 0),
                                   "kind": "backup", "name": mount_name(d), "title_id": ftid,
                                   "platform": fplat, "format": "folder", "is_dir": True})
                    claimed.append(d)
                if claimed:
                    _dirs[:] = [d for d in _dirs if d not in claimed]
                # BASE PACKAGES FIRST. The first PKG parsed for a title id owns the cached cover
                # for ever (see want_icon below), and os.walk hands files over alphabetically -
                # so "Atomfall-Basic-Supply-Bundle-CUSA42503-DLC-..." was parsed before
                # "Atomfall-CUSA42503.pkg" and its pack art became the game's cover on 52 of 56
                # titles here. Names carrying an add-on tag go last within each folder.
                files = sorted(files, key=lambda f: (bool(re.search(r"\b(dlc|update|patch)\b", f, re.I)),
                                                     f.lower()))
                for fn in files:
                    low = fn.lower()
                    is_pkg = low.endswith(".pkg")
                    is_mount = low.endswith(MOUNT_EXTS)
                    if not (is_pkg or is_mount):
                        continue
                    ap = os.path.join(dirpath, fn)
                    try:
                        size = os.path.getsize(ap)
                        key = self._register(ap, reg, sizes)
                    except OSError:
                        # _register() stats the file a second time; a file that vanished between
                        # the two calls (a copy cancelled mid-scan) used to abort the whole scan.
                        continue
                    if is_mount:      # ShadowMount backup - mount lane, no param.sfo
                        mt = re.search(r"(CUSA\d{5}|PPSA\d{5})", fn, re.I)
                        mtid = mt.group(1).upper() if mt else None
                        # "[PS5] PPSA01885 - Evergate.ffpfsc" -> platform from the tag, else from the id
                        if re.search(r"\[\s*PS5\s*\]", fn, re.I):   mplat = "PS5"
                        elif re.search(r"\[\s*PS4\s*\]", fn, re.I): mplat = "PS4"
                        elif mtid:                                    mplat = "PS5" if mtid.startswith("PPSA") else "PS4"
                        else:                                          mplat = "BACKUP"
                        mounts.append({"install_key": key, "file": fn, "size": size, "kind": "backup",
                                       "name": mount_name(fn), "title_id": mtid, "platform": mplat,
                                       "format": os.path.splitext(fn)[1].lower().lstrip(".")})
                        continue
                    tg = re.search(r"(CUSA\d{5}|PPSA\d{5})", fn, re.I)
                    if tg:
                        icon_tid = tg.group(1).upper()
                    else:
                        # No id in the name: ask the package itself before choosing where the
                        # cover goes. It used to be written to <quoted basename>.png, which
                        # has_icon (<TITLE_ID>.png) and /thumb/<tid> never look for - the art was
                        # extracted and the card still showed initials, and because such a name
                        # is "safe" the renamer never fixed it either.
                        try:
                            pre = pkg_meta.parse_pkg(ap)
                        except Exception:
                            pre = None
                        icon_tid = (pre or {}).get("title_id") or key
                    icon_path = os.path.join(ICON_DIR, icon_tid + ".png")
                    want_icon = None if os.path.exists(icon_path) else icon_path   # don't re-extract if cached
                    meta = pkg_meta.parse_pkg(ap, extract_icon_to=want_icon)       # REAL
                    if meta and meta.get("title_id"):
                        n_parsed += 1
                        info = {"title_id": meta["title_id"], "name": meta["title"] or fn,
                                "kind": meta["kind"], "version": meta["app_ver"] or meta["version"],
                                "region": meta["region"], "content_id": meta["content_id"],
                                "icon": bool(meta.get("icon"))}
                    else:
                        n_fallback += 1
                        info = filename_fallback(fn)
                    item = {"install_key": key, "file": fn, "size": size, "kind": info["kind"],
                            "version": info["version"], "name": info["name"], "title_id": info["title_id"],
                            "content_id": info.get("content_id", "")}
                    if info["title_id"]:
                        by_title.setdefault(info["title_id"], []).append(item)
                    else:
                        loose.append(item)

        games = []
        by_tid_entry = {}                   # tid -> the entry, so a backup can merge into it
        for tid, items in by_title.items():
            items = group_parts(items)          # split releases become one entry with `parts`
            base = next((i for i in items if i["kind"] == "base"), items[0])
            by_tid_entry[tid] = {
                "title_id": tid, "name": base["name"], "lane": "install",
                "region": pkg_meta.region_from_content_id(base.get("content_id", "")),
                "platform": "PS5" if str(tid).startswith("PPSA") else "PS4",
                "size": base["size"], "cover_seed": tid,
                "has_icon": os.path.exists(os.path.join(ICON_DIR, tid + ".png")),
                "update_only": not any(i["kind"] == "base" for i in items),
                "base": [i for i in items if i["kind"] == "base"],
                "updates": sorted([i for i in items if i["kind"] == "update"], key=lambda x: x.get("version") or ""),
                "dlc": [i for i in items if i["kind"] == "dlc"], "cheats": []}
            games.append(by_tid_entry[tid])
        for it in loose:
            games.append({"title_id": None, "name": it["name"], "region": "—", "platform": "PS4",
                          "lane": "install", "size": it["size"], "cover_seed": it["file"], "has_icon": False,
                          "base": [it], "updates": [], "dlc": [], "cheats": []})
        seen_mounts = set()
        for m in mounts:      # ShadowMount backup lane (Game Compressor .ffpfsc etc.)
            mtid = m.get("title_id")
            # The same container reaching us twice (two roots, or a peer echoing it back) used to
            # add a second identical card.
            mkey = (mtid or "", str(m.get("file") or ""))
            if mkey in seen_mounts:
                continue
            seen_mounts.add(mkey)
            # ONE CARD PER TITLE. A PS5 game arrives as a .ffpfsc backup while its updates and DLC
            # arrive as .pkg files, so the two halves used to land in different lists and produce
            # two cards for one game - the real one, plus a second named after a content id holding
            # all its add-ons. Merge them: the backup becomes the base, the packages stay as the
            # updates/dlc they now classify as. Verified before the change that NO PS4 title has
            # this split, so nothing on that path can move.
            ex = by_tid_entry.get(mtid) if mtid else None
            if ex is not None:
                ex["base"] = [dict(m, version=None, content_id="")] +                              [i for i in ex.get("base", []) if i.get("kind") != "backup"]
                ex["name"] = m["name"]                     # the real title, not a content id
                ex["platform"] = m.get("platform") or ex.get("platform")
                ex["lane"] = "mount"                       # the GAME installs by mounting
                ex["format"] = m.get("format")
                ex["size"] = m.get("size") or ex.get("size")
                ex["cover_seed"] = mtid or ex.get("cover_seed")
                ex["update_only"] = False
                if not ex.get("has_icon"):
                    ex["has_icon"] = bool(mtid) and os.path.exists(os.path.join(ICON_DIR, mtid + ".png"))
                continue
            games.append({"title_id": mtid, "name": m["name"], "region": "—",
                          "platform": m.get("platform") or "BACKUP",
                          "lane": "mount", "format": m["format"], "size": m["size"],
                          "cover_seed": mtid or m["file"],
                          "has_icon": bool(mtid) and os.path.exists(os.path.join(ICON_DIR, mtid + ".png")),
                          "base": [dict(m, version=None, content_id="")],
                          "updates": [], "dlc": [], "cheats": []})
        games += load_sources(self.cfg)
        # atomic swap: registry / sizes / games all become visible together [B4]
        # THE PS4 APP RIDES THE SAME ROUTE AS EVERY OTHER PACKAGE. Registering it here means it is
        # served by /library/<key> - which already answers HEAD, 200 and 206 properly, which the
        # console's installer requires - instead of needing a second file server that would have to
        # get byte ranges right all over again. It is not a library title: build_library never sees
        # it, so it cannot appear as a game on anyone's shelf.
        if os.path.isfile(PS4_TILE_PKG):
            reg[PS4_TILE_KEY] = PS4_TILE_PKG
            try:
                sizes[PS4_TILE_KEY] = os.path.getsize(PS4_TILE_PKG)
            except OSError:
                pass
        # THE HOMEBREWS RIDE THE SAME ROUTE, FOR THE SAME REASON, AND WITH THE SAME BOUNDARY.
        # Registered here - after `games` is assembled, exactly like the PS4 app above - they are
        # served by /library/<key>, which already answers HEAD, 200 and 206 correctly, so a homebrew
        # installs through the engine the games use instead of needing a second file server.
        #
        # AND THEIR FOLDER IS NEVER A LIBRARY ROOT. It must not go in local_paths: scan() registers
        # every .pkg under a root as a GAME, is_game_folder() accepts the RetroArch folder, and
        # normalise_pkg_names() renames files on disk whose stem is not [A-Za-z0-9._-] - which would
        # rewrite the owner's "PS4-Xplorer 2.0 (LAPY20009) - 2.08.pkg". So the bytes are reached by
        # absolute path, here, and build_library never sees them.
        try:
            # LIVE, for the same reason the panel is: a homebrew replaced or renamed in the
            # folder must be servable under the name it has now, not the one it had at build time.
            _cat, _csig = payload_engine.live_catalog(self.cfg, WEB_DIR)
            for _it in (_cat.get("items") or []):
                if _it.get("kind") != "homebrew":
                    continue
                _p = payload_engine.local_path(self.cfg, _it)
                if not _p:
                    continue
                # A FOLDER-SHAPED APP IS REGISTERED TOO, and that is what makes it installable.
                # It is not served over HTTP - the mount lane pushes it into the drive
                # ShadowMountPlus watches, exactly as it does for a game stored unpacked, and that
                # lane finds its source through this same registry. Skipping it here is why
                # pressing Install on RetroArch could only ever answer "that is a folder".
                _k = payload_engine.serve_key(_it)
                reg[_k] = _p
                try:
                    sizes[_k] = (_it.get("size") or 0) if os.path.isdir(_p) else os.path.getsize(_p)
                except OSError:
                    pass
        except Exception as _e:
            print("[payloads] catalogue not registered: %r" % (_e,))
        self.file_registry, self.file_sizes = reg, sizes
        # str(... or ""): a sources.json entry with no name (or name: null) raised inside this
        # sort, at boot, before the log banner - the frozen exe simply closed.
        self.games = sorted(games, key=lambda g: str(g.get("name") or "").lower())
        self.is_empty = len(self.games) == 0      # honest empty state — never fabricated games
        self.gen += 1
        self.last_scan_ms = now_ms()
        print("[lib] %d title(s) (pkg parsed %d, fallback %d, mount %d) gen=%d%s" %
              (len(self.games), n_parsed, n_fallback, len(mounts), self.gen, " [empty]" if self.is_empty else ""))
        return self.games

    def find_game(self, title_id):
        return next((g for g in self.games if g.get("title_id") == title_id), None)


def load_sources(cfg):
    sf = cfg["library"].get("sources_file")
    if not sf:
        return []
    path = sf if os.path.isabs(sf) else os.path.join(HERE, sf)
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as f:
            raw = json.load(f).get("games", [])
    except Exception as e:
        print("[sources] %s" % e)
        return []
    # Normalise before anything downstream subscripts these. Entries are appended to the library
    # verbatim, and every reader assumes the shape scan() builds: a dict with a string name and
    # the three item lists. One entry without a name used to stop the app from starting at all.
    out = []
    for g in (raw if isinstance(raw, list) else []):
        if not isinstance(g, dict):
            continue
        g = dict(g)
        if not isinstance(g.get("name"), str) or not g["name"].strip():
            g["name"] = str(g.get("title_id") or "Untitled")
            print("[sources] an entry has no name - listing it as %r" % g["name"])
        for k in ("base", "updates", "dlc", "cheats"):
            if not isinstance(g.get(k), list):
                g[k] = []
        g.setdefault("lane", "install")
        out.append(g)
    return out


# --------------------------------------------------------------------------- #
# integrity (SHA-256, cached by path+size+mtime)                               #
# --------------------------------------------------------------------------- #
def load_hashes():
    try:
        with open(HASH_CACHE_PATH) as f:
            return json.load(f)
    except Exception:
        return {}


def save_hashes(d):
    with _state_lock:
        _atomic_write_json(HASH_CACHE_PATH, d)


def sha256_of(path, cache):
    st = os.stat(path)
    ck = "%s|%d|%d" % (path, st.st_size, int(st.st_mtime))
    if ck in cache:
        return cache[ck]
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    cache[ck] = h.hexdigest()
    save_hashes(cache)
    return cache[ck]


def cached_sha(path, cache):
    try:
        st = os.stat(path)
        return cache.get("%s|%d|%d" % (path, st.st_size, int(st.st_mtime)))
    except OSError:
        return None


# --------------------------------------------------------------------------- #
# console-side clients: the legacy CheatRunner service and Payload Manager    #
# --------------------------------------------------------------------------- #
class CheatRunner:
    """OPTIONAL legacy client for the third-party CheatRunner service on the PS5 (:9999).

    Not shipped and not required: OUR on-console ELF implements the whole cheat engine itself
    (Ps5Bridge.mutant_* and mods_action), and every mods route asks it first. This class is kept
    only as a last-resort fallback for someone who still runs CheatRunner beside the shop, and it
    is only ever consulted after our engine has said it has no cheat file for the title.
    alive() caches its answer both ways so a missing service costs nothing on the hot paths.

    Endpoints (verified live against v0.16):
      GET /api/cheats/find?titleId=            -> best local file + scored candidates
      GET /api/cheats?titleId=                 -> the loaded cheat doc (mods[])
      GET /api/cheats/state?titleId=&debug=1   -> which mods are currently on
      POST /api/cheats/select?titleId=&path=[&force=1]
      GET /api/cheats/toggle?titleId=&index=&on=0|1
      GET /api/cheats/disable-all?titleId=
      GET /api/patches/apply?titleId=&index=|&entryId=[&force=1]
    """
    PORT = 9999

    def __init__(self, ip, port=None, timeout=20):
        self.ip = ip
        self.port = int(port or self.PORT)
        self.timeout = timeout

    def _call(self, path, method="GET", timeout=None):
        url = "http://%s:%d%s" % (self.ip, self.port, path)
        req = urllib.request.Request(url, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self.timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace") or "{}")
        except urllib.error.HTTPError as e:
            try:
                return json.loads(e.read().decode("utf-8", "replace") or "{}")
            except Exception:
                return {"ok": False, "error": "http_%s" % e.code}
        except Exception as e:
            return {"ok": False, "error": repr(e), "unreachable": True}

    def alive(self, ttl=15.0, probe=True):
        """Is CheatRunner reachable? Cached BOTH ways for `ttl` seconds.

        The uncached version cost 2.04 s every single call, because :9999 is normally not
        listening and Windows burns the full connect timeout on a refused TCP connect. Measured:
        GET /api/mods/<tid> for a title with no local cheat file took 2.090 s, against 0.059 s for
        a title that has one - the whole delta was this probe. It is also why /api/health went to
        2.1 s once every ten minutes, when running_title()'s negative memo expired and re-probed.

        Caching the FALSE is the point, so it is deliberately keyed off the RESULT and not off an
        exception: _call() swallows every error and returns {"ok": False, "unreachable": True},
        so a try/except memo would never arm.

        A short symmetric TTL beats a long negative memo: someone who starts CheatRunner is
        picked up within `ttl` instead of being told it is offline for the next ten minutes.
        """
        now = time.monotonic()
        if now - getattr(self, "_alive_ts", 0.0) < ttl:
            return getattr(self, "_alive_val", False)
        if not probe:
            return getattr(self, "_alive_val", False)
        v = bool(self._call("/api/health", timeout=1.5).get("ok"))
        self._alive_ts, self._alive_val = now, v
        return v

    def running(self):
        """What game is running right now: {titleId,titleName,appVersion,pid,...} or {}."""
        st = self._call("/api/state", timeout=6)
        r = (st.get("running") or {}) if isinstance(st, dict) else {}
        return r if r.get("running") else {}

    def find(self, tid):
        return self._call("/api/cheats/find?titleId=" + quote(tid))

    def doc(self, tid):
        return self._call("/api/cheats?titleId=" + quote(tid))

    def state(self, tid):
        return self._call("/api/cheats/state?titleId=%s&debug=1" % quote(tid))

    def select(self, tid, path, force=False):
        return self._call("/api/cheats/select?titleId=%s&path=%s%s"
                          % (quote(tid), quote(path), "&force=1" if force else ""), method="POST")

    def toggle(self, tid, index, on):
        return self._call("/api/cheats/toggle?titleId=%s&index=%d&on=%d"
                          % (quote(tid), int(index), 1 if on else 0))

    def disable_all(self, tid):
        return self._call("/api/cheats/disable-all?titleId=" + quote(tid))

    def apply_patch(self, tid, index=None, entry_id=None, force=False):
        idpart = ("&entryId=" + quote(str(entry_id))) if entry_id else ("&index=%d" % int(index or 0))
        return self._call("/api/patches/apply?titleId=%s%s%s"
                          % (quote(tid), idpart, "&force=1" if force else ""), timeout=60)


def _merge_mod(i, m, srow):
    """Combine the cheat document entry with its live engine state."""
    state = (srow.get("state") or "").lower()
    return {
        "index": i,
        "name": m.get("name") or m.get("title") or ("Mod %d" % (i + 1)),
        "type": m.get("type") or "",
        "patches": m.get("patches"),
        "state": state or "off",
        "label": srow.get("label") or ("ON" if state == "on" else "OFF"),
        "on": state == "on",
        "conflict": state == "conflict",
        "can_toggle": bool(srow.get("canToggle", True)),
        "next_on": bool(srow.get("nextOn", state != "on")),
        "conflicts_with": [c.get("name") for c in (srow.get("conflictsWith") or []) if c.get("name")],
    }


def _cheat_format(path):
    """Which of our three cheat formats a file is. .shn/.mc4 are Trainer XML (the latter
    AES-encrypted); the ELF converts both to the same document shape we already parse."""
    low = (path or "").lower()
    for ext in ("shn", "mc4", "json"):
        if low.endswith("." + ext):
            return ext
    return "json"


def cheat_file_version(filename):
    """Pull the version out of a cheat filename: CUSA14409_01.04.json -> "01.04"."""
    m = re.search(r"_(\d{2}\.\d{3}\.\d{3})", filename or "")   # PS5: 01.011.000
    if m:
        return m.group(1)
    m = re.search(r"_(\d{2}\.\d{2})", filename or "")            # PS4: 01.04
    return m.group(1) if m else ""


class PayloadManager:
    """Client for Payload Manager (pldmgr, :8084) on the console, over LAN.

    Two jobs today, both ours:
      * It is how each install is STARTED. The on-console ELF writes installer-req.txt and asks
        pldmgr to spawn pms-installer.elf, because sceAppInstUtilInstallByPackage only succeeds
        from a freshly spawned process (see Ps5Bridge.install_spawn). From this side, alive()
        is therefore the "can an install start" question that /api/health and /api/engine/state
        report as engine_ready.
      * restart_shadowmount() uses kill()/load() to reload ShadowMount after its register-path
        config is fixed, when no game is running.
    It used to reload a third-party install daemon that wedged after heavy installs; there is no
    such daemon in the path any more and nothing here reloads anything on its own.

    Measured on-device (FW 12.70, pldmgr v0.5.0 on :8084): kill -> process gone in ~2.5 s,
    loadpayload -> serving again in ~3.5 s. /loadpayload resolves by BASENAME to pldmgr's own copy
    (see the memory note on that trap), so always check /api/health after a load.
    """
    PORT = 8084

    def __init__(self, ip, port=None, timeout=25):
        self.ip = ip
        self.port = int(port or self.PORT)
        self.timeout = timeout

    def _get(self, path, timeout=None):
        url = "http://%s:%d%s" % (self.ip, self.port, path)
        with urllib.request.urlopen(url, timeout=timeout or self.timeout) as r:
            return r.read().decode("utf-8", "replace")

    def alive(self, ttl=8.0, probe=True):
        """Is Payload Manager reachable? (auto-recovery is only possible when it is)

        Cached for `ttl` seconds. This is asked on paths that must stay quick — most importantly
        /api/federation, which is how OTHER PCs identify us. With the console off, the bare probe
        cost 3s every time, which pushed our identity reply past the timeout other companions
        allow: we became invisible to them for no reason other than the PS5 being asleep.
        """
        now = time.monotonic()
        if now - getattr(self, "_alive_ts", 0.0) < ttl:
            return getattr(self, "_alive_val", False)
        if not probe:
            return getattr(self, "_alive_val", False)   # answer from memory, never stall a caller
        try:
            v = bool(self._get("/version", timeout=3).strip())
        except Exception:
            v = False
        self._alive_ts, self._alive_val = now, v
        return v

    def processes(self):
        try:
            return json.loads(self._get("/processes_list", timeout=8)).get("processes", [])
        except Exception:
            return []

    def pids_named(self, prefix):
        pre = prefix.lower()
        return [p.get("pid") for p in self.processes()
                if str(p.get("name", "")).lower().startswith(pre) and p.get("pid")]

    def payload_path(self, needle):
        """Full on-console path of a payload whose filename contains `needle` (e.g. 'arsenal')."""
        try:
            paths = json.loads(self._get("/list_payloads", timeout=8)).get("payloads", [])
        except Exception:
            return None
        n = needle.lower()
        for p in paths:
            if n in p.rsplit("/", 1)[-1].lower():
                return p
        return None

    def kill(self, pid):
        try:
            self._get("/process_kill?pid=%d" % int(pid), timeout=10)
            return True
        except Exception:
            return False

    def load(self, path):
        try:
            self._get("/loadpayload:" + quote(path, safe="/:"), timeout=30)
            return True
        except Exception:
            return False


# --------------------------------------------------------------------------- #
# console bridge + fleet                                                        #
# --------------------------------------------------------------------------- #
class Ps5Bridge:
    LOC_LABEL = {"0": "Internal SSD", "2": "Extended Storage"}

    def __init__(self, console, cfg):
        self.c = console
        self.cfg = cfg
        self._apps = None
        self._apps_ts = 0.0
        self._backups = None
        self._backups_ts = 0.0
        self._ps4meta = None       # PS4/CUSA titles proven installed by their own app.pkg
        self._ps4loc = {}          # ...and which root each was found under
        self._ps4meta_ts = 0.0
        self._apps_refreshing = False   # guards the background stale-while-revalidate refresh
        self._pldmgr = None        # Payload Manager client — this is how our installer is spawned
        self._cr = None            # CheatRunner client — mods / cheats / patches
        self._run_cache, self._run_ts = {}, 0.0
        self._addons = None        # content_ids from addcont.db — the only record a DLC leaves
        self._addons_ts = 0.0
        self._ftp_port = None      # sticky: which FTP port answered last. Probe, never assume -
                                   # the port depends on which FTP payload the user runs.
        self._fs_ok, self._fs_ok_at = None, 0.0   # is OUR on-console file API answering?
        self._seen_platform = ""   # "ps4"/"ps5" as the console itself reported it — see platform_id()
        self._plat_probe_at = 0.0

    # FTP is the FALLBACK transport, not the route. Every read of console state - app.db, bgft.db,
    # addcont.db, the install proof, the cheat sync - goes through fs_read/fs_list, which ask OUR
    # on-console ELF's /api/fs first and only come here when it is not loaded. Which FTP payload
    # the user runs decides the port (ftpsrv on :2121, others on :1337), so try the configured one
    # and fall back to the other rather than going blind. The working port is remembered so this
    # costs one connect in the normal case.
    FTP_FALLBACKS = (2121, 1337)

    def _ftp(self, timeout=6):
        first = self._ftp_port or self.c.get("ftp_port") or 2121
        order = [first] + [p for p in self.FTP_FALLBACKS if p != first]
        last = None
        for port in order:
            ftp = ftplib.FTP()
            try:
                ftp.connect(self.ip, port, timeout=timeout)
                ftp.login()
            except Exception as e:
                last = e
                try:
                    ftp.close()
                except Exception:
                    pass
                continue
            if self._ftp_port != port:
                if self._ftp_port is not None:
                    print("[ftp] %s: port %s -> %s" % (self.name, self._ftp_port, port))
                self._ftp_port = port
            return ftp
        self._ftp_port = None          # both gone - re-probe from the configured port next time
        raise last if last else OSError("no FTP port reachable")

    # ---------------- our on-console engine: cheats, patches, the console's own scans -------------
    def mutant_running(self, version=""):
        """Running game as OUR on-console engine sees it (title/pid/base/cheat file).

        Pass the installed version when we know it so the ELF can prefer the exactly
        matching cheat file instead of falling back to "some other version"."""
        q = "/api/cheat/running"
        if version:
            q += "?version=" + quote(str(version))
        try:
            j = self._shop(q, timeout=6)
            return j if j.get("running") else {}
        except Exception:
            return {}

    USB_PKG_TTL = 20.0

    def console_usb_packages(self):
        """Installable packages the CONSOLE can see on its own removable media.
        Our on-console app already scans /mnt/usb0..7, so ask it rather than
        duplicating the scan here (two scanners would drift apart).

        Cached for USB_PKG_TTL seconds per bridge. This was the one uncached call in
        build_library(): every /api/library on every device - and the peer-hosted /api/install
        and /api/transfer, which rebuild the federated library - made the console rebuild its
        whole library JSON (app.db read, eight USB scans) on its single accept loop. A stick
        plugged in shows up within 20 s, which nobody notices; the console does."""
        now = time.time()
        memo = getattr(self, "_usbpkg_memo", None)
        if memo and now - memo[0] < self.USB_PKG_TTL:
            return [dict(g) for g in memo[1]]
        try:
            doc = self._shop("/api/library", timeout=20) or {}
        except Exception:
            return []
        out = []
        for g in (doc.get("games") or []):
            if not str(g.get("source") or "").startswith("usb"):
                continue
            g = dict(g)
            g["lane"] = "install"
            g["on_console"] = False
            out.append(g)
        self._usbpkg_memo = (now, out)
        return [dict(g) for g in out]

    def console_usb_packages_cached(self):
        """What the last USB sweep of THIS console found, without asking it again. None if never.

        /api/install needs to know which console a "local:" path belongs to, and it must not pay a
        20 s library rebuild per console to find out - the Install button has a 60 s clock on it and
        a console that is asleep spends every one of those seconds timing out. No TTL here on
        purpose: build_library() asks EVERY console in one sweep, so the memo behind an item the
        owner can see was filled by the same listing that put it on screen. A console that could not
        be reached during that sweep never reached the line above, so it has no memo at all and
        answers None - which the caller reads as "no evidence", not as "not here"."""
        memo = getattr(self, "_usbpkg_memo", None)
        return [dict(g) for g in memo[1]] if memo else None

    def cheat_paths(self):
        """Where the console keeps cheats, and which folders it watches for drop-ins."""
        try:
            return self._shop("/api/cheat/paths", timeout=8)
        except Exception:
            return {}

    def engine_agent(self, ttl=5.0):
        """Is the in-game helper installed, listed, and currently inside a game?

        PS4 only in practice - a PS5 needs no helper because its payload reaches game memory by
        itself - but it is not gated on platform here. A console that does not serve the route
        answers with its own {} and the caller sees that, which is better than this file deciding
        in advance what a console can do. That assumption is exactly what kept the PS4's cheat
        routes unreachable while the console was serving them perfectly well.

        CACHED BRIEFLY, because the Cheats panel polls while it is open: without this, every poll
        would add a console round trip to answer a question whose answer only changes when somebody
        arms or purges something. Five seconds is far shorter than any arming action and far longer
        than the panel's own interval."""
        now = time.time()
        if now - getattr(self, "_agent_at", 0) < ttl:
            return getattr(self, "_agent_cache", {})
        try:
            d = self._shop("/api/engine/agent", timeout=8) or {}
        except Exception:
            d = {}
        self._agent_cache, self._agent_at = d, now
        return d

    def engine_agent_set(self, enabled, title=None, all_games=False):
        """List or unlist the helper.

        `title` lists it for ONE game by appending a [TID] section, which touches no existing line and
        so cannot affect any other game or any other plugin the owner runs. That is the path the
        Cheats panel uses. `all_games` is the blanket [default] form, and the console refuses it
        unless it is asked for by name - leaving it out used to be enough to arm every game.
        """
        body = {"enabled": 1 if enabled else 0}
        if title:
            body["title"] = title
        if all_games:
            body["all_games"] = 1
        try:
            return self._shop_post("/api/engine/agent", body, timeout=20)
        except Exception:
            return {}

    def cheat_rescan(self):
        """File anything dropped in since boot, without reloading the payload."""
        try:
            return self._shop("/api/cheat/rescan", timeout=60)
        except Exception:
            return {}

    def mutant_find(self, title_id, version=""):
        """Cheat file OUR engine would use for a title, running or not. This is what keeps
        mod browsing working without CheatRunner."""
        q = "/api/cheat/find?title=" + quote(str(title_id))
        if version:
            q += "&version=" + quote(str(version))
        try:
            j = self._shop(q, timeout=8)
            return j if j.get("ok") else {}
        except Exception:
            return {}

    def mutant_mods(self, cheat_file, pid=None, base=None):
        """Mods plus their LIVE on/off state (needs pid+base, else state is unknown)."""
        q = "/api/cheat/list?file=" + quote(cheat_file)
        if pid:
            q += "&pid=%d" % int(pid)
            if base:
                q += "&base=%s" % (base if isinstance(base, str) else hex(base))
        try:
            return self._shop(q, timeout=25)
        except Exception:
            return {}

    def mutant_apply(self, cheat_file, index, on, pid, base, name="", force=False):
        q = ("/api/cheat/apply?file=%s&mod=%d&on=%d&pid=%d&base=0x%x&name=%s%s"
             % (quote(cheat_file), int(index), 1 if on else 0, int(pid), int(base),
                quote(name[:60]), "&force=1" if force else ""))
        try:
            return self._shop(q, timeout=90)
        except Exception as e:
            return {"ok": False, "error": repr(e)}

    def mutant_patches(self, title_id, version=""):
        """Game patches OUR engine holds for a title (the XML patch library).

        Separate from cheats: a patch is written once rather than toggled, comes from
        <patches>/<TITLEID>.xml, and a title can have patches with no cheat file at all — so this
        is asked for independently of the cheat document.
        """
        q = "/api/patch/list?title=" + quote(str(title_id))
        if version:
            q += "&version=" + quote(str(version))
        try:
            j = self._shop(q, timeout=15)
            return j.get("patches") or []
        except Exception:
            return []

    def mods_action(self, title_id, action, body, timeout=60):
        """Run a mod action (select / toggle / apply / disable-all) through OUR engine.

        The on-console ELF implements all four itself — it resolves the cheat file, gates each
        write on the memory already holding the expected bytes, and raises the on-screen toast.
        Proxying to it keeps ONE implementation of the engine instead of a second, drifting copy
        here; this is also the only path that works when the PC is driving a game on the console.

        Returns the console's reply as-is (including its own honest refusals, e.g.
        `game_not_running`), or None when our engine could not be reached at all.
        """
        try:
            return self._shop_post("/api/mods/%s/%s" % (quote(str(title_id)), action),
                                   body or {}, timeout=timeout)
        except Exception:
            return None

    def running_title(self, ttl=4.0):
        """Title id of the game running on the console right now (cached briefly)."""
        now = time.time()
        if now - getattr(self, "_run_ts", 0) < ttl:
            return getattr(self, "_run_cache", {})
        # NOT ON A PS4, and this costs real time rather than merely being wrong.
        #
        # Both answers below are PS5 machinery. /api/cheat/running is our cheat engine, which walks
        # process memory through a kernel read/write the PS4 port does not have - the PS4's server
        # answers that path with 200 and an empty body, so it looks like "no game running" rather
        # than "cannot be asked". Falling through, the next attempt is CheatRunner on :9999, which
        # nothing on a PS4 is listening on: on Windows a refused connect to a live host is measured
        # at ~2 s, and this sits on the health path.
        #
        # The memo below hides it most of the time and then pays it again every ten minutes, on
        # whichever poll happens to land after the timer expires.
        #
        # THE PS4 CAN ANSWER THIS NOW, and this is the "where that goes" the old comment here
        # promised. Its payload reports the foreground title in /api/health (measured signatures -
        # sceSystemServiceGetAppIdOfBigApp plus a process walk), and its in-game agent supplies the
        # pid and image base a cheat needs. mutant_running() asks the console's own engine, which
        # is the same question for both platforms, so there is nothing PS4-specific left to skip.
        r = self.mutant_running()          # our engine first
        if r:
            r = {"titleId": r.get("title_id"), "titleName": "", "pid": r.get("pid"),
                 "imageBase": r.get("base"), "running": True}
        else:
            # CheatRunner was removed from this project; our own engine answers in ~15ms. Asking for
            # it anyway cost a refused connection — 2.07s on Windows — on EVERY health check. That
            # pushed /api/health past the 1.5s the console allows when looking for a companion, so
            # the PS5 never found one and fell back to its own API, which can only install packages
            # already on the console. One dead call was breaking installing from a PC entirely.
            # Kept as a fallback for anyone still running it, but only retried occasionally.
            if now - getattr(self, "_cr_fail_ts", 0) < 600:
                r = {}
            else:
                try:
                    # alive() first (1.5 s, cached both ways) rather than the 6 s /api/state
                    # call straight away: the once-per-600 s re-probe landed on a health poll and
                    # stalled it for the full refused-connect timeout.
                    r = self.cheats.running() if self.cheats.alive() else {}
                except Exception:
                    r = {}
                # Its client swallows every error and returns a dict, so an exception never arrives
                # here — the memo has to key off the RESULT. Nothing back means nothing to ask, and
                # our own engine has already answered the "which game is running" question anyway.
                self._cr_fail_ts = 0.0 if r else now
        self._run_cache, self._run_ts = r, now
        return r


    @property
    def cheats(self):
        if getattr(self, "_cr", None) is None:
            self._cr = CheatRunner(self.ip, self.cfg.get("cheats", {}).get("port", CheatRunner.PORT))
        return self._cr

    @property
    def pldmgr(self):
        if self._pldmgr is None:
            port = self.cfg.get("dpi", {}).get("pldmgr_port", PayloadManager.PORT)
            self._pldmgr = PayloadManager(self.ip, port)
        return self._pldmgr

    @property
    def name(self):
        return self.c["name"]

    @property
    def ip(self):
        return self.c["ip"]

    def ftp_ok(self, timeout=1.2):
        """Is ANY console FTP up? Same 2121/1337 question as _ftp() - probing only the configured
        port reported "FTP down" the moment Arsenal's ftpsrv stopped, while etaHEN's 1337 was
        serving perfectly and the app was happily reading app.db over it."""
        if not self.ip:
            return False
        first = self._ftp_port or self.c.get("ftp_port") or 2121
        for port in [first] + [p for p in self.FTP_FALLBACKS if p != first]:
            try:
                with socket.create_connection((self.ip, port), timeout=timeout):
                    self._ftp_port = port
                    return True
            except OSError:
                continue
        return False

    def notify(self, text):
        """Put a message on the television, through our own ELF's /api/notify. True if it took it.

        This used to open a socket to console.notify_port (9099) and nothing else. Nothing listens
        on 9099 - it is not bound by this app or by any payload we ship - so every call quietly
        returned False and no notification was ever sent from the PC. The 9099 socket stayed on as
        a "legacy" fallback after /api/notify arrived and only ever cost a 2 s connect timeout per
        missed toast; it is gone.

        Capped at 250 bytes of UTF-8, cut at a character boundary: the console keeps the text in
        a 300-byte buffer and would otherwise cut a longer one mid-character.
        """
        if not self.ip:
            return False
        s = str(text).replace("\n", " ")
        s = s.encode("utf-8")[:250].decode("utf-8", "ignore")
        try:
            j = self._shop("/api/notify?text=%s" % quote(s, safe=""), timeout=6) or {}
            return bool(j.get("ok"))
        except Exception:
            return False

    def open_shop(self, url=None):
        """Ask the console to open the shop in its own browser. Returns the console's own answer.

        Returns a dict now, not a bool: whether the browser actually opened and whether the console
        at least put the address on screen are different outcomes and the user deserves to be told
        which one happened. sceSystemServiceLaunchWebBrowser is resolved by name on the console, so
        a firmware that does not export it degrades to the notification rather than to silence.
        """
        if not self.ip:
            return {"ok": False, "error": "no console configured"}
        try:
            q = "/api/open" + ("?url=%s" % quote(str(url), safe="") if url else "")
            j = self._shop(q, timeout=8) or {}
            if j:
                return j
        except Exception:
            pass
        # No 9099 fallback any more. It could answer {ok:true, launched:true} for ANYTHING that
        # accepted the TCP connect - nothing of ours ever listened there - which is a fabricated
        # "the browser opened" for a console that did nothing.
        return {"ok": False, "error": "PKG MUTANT SHOP on the PS5 did not answer - "
                                      "load it again from Payload Manager"}

    # ---------------- our own install engine (on-console, no Elf Arsenal) -------------
    def _shop(self, path, timeout=30, data=None):
        """Call the on-console shop server (our ELF). `data` (a dict) makes it a POST.

        An HTTP error status from the console is NOT a failure to reach the console, and the
        difference matters enormously: on 2026-08-25 a 409 "an install is already running" was
        reported as "the console did not answer - load it again from Payload Manager", the ELF was
        reloaded on that advice, and reloading zeroed the very latch that was refusing the
        duplicate. Callers that care can now see the status and the console's own words.
        """
        # Ask the cheap question first. Without this, every proxied call to a console that is
        # switched off waits out its full HTTP timeout.
        if not self.up():
            raise IOError("the console is not answering on the network")
        port = self.cfg.get("console", {}).get("shop_port", 8710)
        url = "http://%s:%d%s" % (self.ip, port, path)
        req = url
        if data is not None:
            req = urllib.request.Request(url, data=json.dumps(data).encode("utf-8"),
                                         headers={"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:
                pass
            try:
                j = json.loads(body)
            except ValueError:
                j = {}
            raise ShopHTTPError(e.code, j, body, url)

    # Any answer that identifies the console teaches us its platform, so the lazy probe in
    # platform_id() almost never has to fire: /api/health goes through here from the UI poll, the
    # queue and engine_available() alike.
    def _shop_text(self, path, timeout=15):
        """GET a PLAIN TEXT endpoint on the console. _shop() json.loads() its answer, which is
        exactly wrong for the install log - the only thing it could ever return is an exception."""
        # Same cheap gate as _shop(): without it a log read against a switched-off console sat
        # out the whole HTTP timeout before the 502.
        if not self.up():
            raise IOError("the console is not answering on the network")
        url = "http://%s:%d%s" % (self.ip, self.cfg.get("console", {}).get("shop_port", 8710), path)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            raise self._as_shop_error(e, url)

    @staticmethod
    def _as_shop_error(e, url):
        """An HTTPError from the console, as the ShopHTTPError _shop() raises - so a 4xx/5xx
        with the console's own sentence in it is never mistaken for "did not answer"."""
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:
            pass
        try:
            j = json.loads(body)
        except ValueError:
            j = {}
        return ShopHTTPError(e.code, j, body, url)

    def _shop_post(self, path, payload, timeout=120):
        """POST to the on-console shop. Same reachability gate and same error mapping as _shop().

        The default used to be 900 s "because installs from a stick can take minutes" - but the
        console answers POST /api/install immediately and installs on its own thread
        (server.c: pthread_create(localinst_thread) then send_json), and every other caller
        passes its own timeout. Nine hundred seconds only ever bought a worker sitting in
        "submitting" against a console that accepted TCP and never answered."""
        # A console that is off should be found out in milliseconds, exactly as _shop() does,
        # not after a mod toggle has hung the request for minutes.
        if not self.up():
            raise IOError("the console is not answering on the network")
        port = self.cfg.get("console", {}).get("shop_port", 8710)
        url = "http://%s:%d%s" % (self.ip, port, path)
        data = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8", "replace"))
        except urllib.error.HTTPError as e:
            raise self._as_shop_error(e, url)

    def install_local_on_console(self, path, name=""):
        """Install a package that is ALREADY on the console (USB stick, external drive).
        Nothing is transferred, so this bypasses the download/DPI lane entirely."""
        return self._shop_post("/api/install", {"install_key": "local:" + path,
                                                "path": path, "name": name})

    def helper_status(self):
        """Live state of the two helper services, for the settings panel.

        ShadowMount binds loopback-only, so from the PC we can only report it as 'unknown'
        rather than pretending it is down — the on-console shop answers this accurately.

        MEMOISED per bridge. /api/health said "cached helper state, so adding these two keys
        costs health nothing" and it was not: every 6 s poll from every device spawned an FTP probe
        thread and a /api/helpers round trip to the console's single accept loop - and the memo was
        set to 5 s against a 6 s poll, so it expired before each poll and caught none of them. It
        is HEALTH_MEMO_S now, which is longer than the poll rather than shorter."""
        now = time.monotonic()
        memo = getattr(self, "_helpers_memo", None)
        if memo and now - memo[0] < HEALTH_MEMO_S:
            return dict(memo[1])
        smp = self.cfg.get("shadowmount", {}).get("port", 10101)
        # ShadowMount binds LOOPBACK-ONLY on the console, so probing it from the PC can never
        # succeed - it can only burn its full timeout. Measured: this endpoint took 771 ms to
        # return 564 bytes, three sequential 0.7 s probes deep, and the on-console answer below
        # overwrote the ShadowMount value anyway. The UI calls this from updateSrcBar() on every
        # successful library load, so it sat on the boot path for three quarters of a second.
        # Do not probe what cannot answer; run the two that can, side by side.
        res = {}

        def _probe_ftp():
            # NOT a single-port probe. Which FTP is up depends on which host is running (Arsenal's
            # ftpsrv on 2121, etaHEN's own on 1337), so this asks the same question _ftp() asks and
            # leaves self._ftp_port pointing at whichever answered.
            res["ftp"] = self.ftp_ok(timeout=1.2)

        # Only FTP is worth probing from here. The "install host" probe that used to run beside it
        # asked whether a third-party daemon was on :12800; there is no such daemon, and the port it
        # read was removed from the console dict by the purge - so this function raised KeyError and
        # its caller quietly turned that into "ShadowMount is not running".
        th = threading.Thread(target=_probe_ftp, daemon=True)
        th.start()
        th.join(timeout=2.5)
        # shadowmount starts as None = UNKNOWN, not False. It binds loopback-only, so from the PC
        # the honest answer is "cannot tell from here" - the console's own answer replaces it below
        # whenever it is reachable.
        out = {"shadowmount": None, "shadowmount_port": smp,
               "ftp": res.get("ftp", False),
               "ftp_port": self._ftp_port or self.c.get("ftp_port") or 2121}
        # ShadowMount listens on loopback only, so a LAN probe always says "down". Our on-console
        # shop can see it properly — prefer its answer whenever it is reachable.
        try:
            j = self._shop("/api/helpers", timeout=4)
            if j.get("ok"):
                for k in ("shadowmount", "ftp"):
                    if k in j:
                        out[k] = bool(j[k])
                # The console knows the real port numbers; the PC only has config. ShadowMount in
                # particular moved (see the shadowmount default below) and a stale config here is
                # what put ":9021 - active" under a service that is not on 9021.
                for k in ("shadowmount_port",):
                    if j.get(k):
                        out[k] = int(j[k])
                out["source"] = "console"
        except Exception:
            out["source"] = "lan"
        self._helpers_memo = (now, dict(out))
        return out

    def engine_available(self):
        try:
            h = self._shop("/api/health", timeout=4)
        except Exception:
            return False
        self._note_platform(h)
        return bool(h.get("on_console"))

    # ------------------------------------------------- which console is this, really
    #
    # The PS4 payload reports `platform: "ps4"` in /api/health. The PS5 payload has never had that
    # field and is not being changed to add one, so a health answer from a console that does NOT
    # name a platform is a PS5. That asymmetry is deliberate: it means PS4 support needed no edit
    # to the shipping PS5 build.
    #
    # A platform written in config.json wins, so a console that is switched off is still handled
    # correctly. Anything learned from the console is remembered for the life of the process but
    # never written to disk from here - config.json is saved in one place, by main().
    def _note_platform(self, health):
        if not isinstance(health, dict) or not health.get("on_console"):
            return
        seen = str(health.get("platform") or "ps5").strip().lower()
        if seen in ("ps4", "ps5") and self._seen_platform != seen:
            self._seen_platform = seen

    def platform_id(self):
        """"ps4" or "ps5". Configured value first, then whatever the console said, then "ps5"."""
        cfgd = str(self.c.get("platform") or "").strip().lower()
        if cfgd in ("ps4", "ps5"):
            return cfgd
        if not self._seen_platform and time.time() - self._plat_probe_at > 10:
            # ASK, ONCE, RATHER THAN ASSUME. Callers reach here on paths that have not touched the
            # console yet, and defaulting an unknown console to "ps5" would send a PS4 down the PS5
            # lane. This is the same /api/health the UI polls, so it is warm in practice; a console
            # that is switched off simply stays unknown until it answers.
            self._plat_probe_at = time.time()
            try:
                self._note_platform(self._shop("/api/health", timeout=3))
            except Exception:
                pass
        return self._seen_platform or "ps5"

    def is_ps4(self):
        return self.platform_id() == "ps4"

    # ------------------------------------------------- what to CALL this console in a sentence
    #
    # Every failure the install lane can report named "the PS5" in the literal, so a PS4 owner
    # watching a PS4 game fail was told to check a machine they may not even own. bridge.name is
    # not the fix on its own: it is the owner's free text, and consoles_from_cfg's legacy
    # single-console fallback hardcodes it to "PS5" for any config written before PS4 support,
    # whatever the console turns out to be. These two read platform_id(), which asks the console.
    def kind_label(self):
        """"PS4" or "PS5", for a sentence a person reads."""
        return "PS4" if self.is_ps4() else "PS5"

    def reload_suffix(self):
        """Where the shop is loaded from, when that differs by console.

        Payload Manager is a PS5 payload; the PS4 loads payloads another way, so telling a PS4
        owner to use it names a recovery they do not have. The jailbreak software is deliberately
        not named in its place - the house style bans naming software this app does not ship, and
        the owner already knows how they load payloads on their own console."""
        return "" if self.is_ps4() else " from Payload Manager"

    # install_pms() lived here: the in-process /api/engine/install-url lane, which registers with
    # sceAppInstUtilAppInstallPkg (metadata only - a tile that crashes). Nothing called it from
    # this side any more, and on the console that route is a diagnostic behind allow-diagnostics.
    # The one lane is install_spawn().

    def fetch_to(self, url, dest, name, timeout=60, size=0):
        """Ask the console to DOWNLOAD a file into `dest` under exactly `name`, and nothing else.

        This is the PS5 backup lane: our own downloader pulls the container into the drive's
        homebrew folder and ShadowMount mounts it. The console writes "<name>.part" and renames only
        once the last byte is across, because ShadowMount mounts whatever appears in that folder and
        a half-written container mounts as a broken game.

        Returns (ok, info). ok=False with info["unsupported"] means this console is running an ELF
        from before the endpoint existed - the caller should fall back to pushing.
        """
        q = "/api/engine/fetch?url=%s&dest=%s&name=%s" % (
            quote(url, safe=""), quote(dest, safe=""), quote(name, safe=""))
        # Tell it how big this is, so it can refuse a drive without room BEFORE the transfer rather
        # than filling one. The "internal" destination is /data/homebrew on the system partition,
        # which is exactly where a 100 GB backup must never silently land.
        if size:
            q += "&size=%d" % int(size)
        try:
            j = self._shop(q, timeout=timeout) or {}
        except ShopHTTPError as e:
            return False, {"error": e.sentence("The console refused the download"),
                           "http_status": e.status}
        except Exception as e:
            return False, {"error": "PKG MUTANT SHOP on the PS5 did not answer", "detail": repr(e)[:200]}
        if not j:
            # The unknown-/api/ stub answers {} - i.e. this ELF predates /api/engine/fetch.
            return False, {"unsupported": True,
                           "error": "This console is running an older PKG MUTANT SHOP"}
        if not j.get("ok"):
            return False, {"error": j.get("error") or "The console would not start the download"}
        return True, j

    def engine_job(self):
        try:
            return self._shop("/api/engine/job", timeout=20)
        except Exception:
            return None

    # THE PS4 NEEDS THE PACKAGE DESCRIBED UP FRONT; THE PS5 DOES NOT.
    #
    # The PS5 lane hands over a URL and the console reads the package header itself. The PS4's
    # background transfer service refuses a task that does not already carry the CONTENT id and the
    # real byte size: a title id where the content id belongs answers 0x80990008, and a zero size is
    # refused outright. It cannot read a header it has not downloaded yet - but this machine parsed
    # that header when it scanned the library, so the values cost nothing to pass on.
    #
    # Sent to BOTH consoles on purpose. The PS5 build's route reads `uri` and `name` and ignores
    # every other parameter, so this cannot change what the PS5 does, and one code path here will
    # not drift the way a platform branch would.
    @staticmethod
    def _pkg_query(pkg):
        if not isinstance(pkg, dict):
            return ""
        q = ""
        cid = str(pkg.get("content_id") or "").strip()
        if cid:
            q += "&cid=" + quote(cid, safe="")
        try:
            size = int(pkg.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > 0:
            q += "&size=%d" % size
        ptype = PS4_PACKAGE_TYPE.get(str(pkg.get("kind") or "base").strip().lower())
        if ptype:
            q += "&type=" + ptype
        return q

    def install_spawn(self, url, name="", force=False, cancelled=None, pkg=None):
        """OUR OWN base-game lane. No etaHEN anywhere in the call.

        `cancelled` is an optional callable the queue passes so a cancel pressed while this waits
        for the lane to free up stops the hand-over BEFORE the console is asked for anything;
        the answer then carries `canceled: True` and nothing was submitted.

        sceAppInstUtilInstallByPackage returns 0x80B2116F when called from a payload injected into
        a hijacked host process - which is what our shop ELF is - and succeeds from a freshly
        SPAWNED process. So the ELF writes the URI to a request file, has Payload Manager spawn
        pms-installer.elf, and that process makes the call. Proven on hardware 2026-08-25:
        Castle Crashers (CUSA14409, absent beforehand) -> bgft 1036, title "PKG MUTANT SHOP".

        Returns (ok, info) with the same shape every other lane uses. `ok` means the console
        ACCEPTED the package and created a BGFT task - completion is still proven from bgft.db,
        exactly as it is for etaHEN.
        """
        # DOUBLE-SUBMIT DEBOUNCE, and nothing more.
        #
        # This used to remember accepted URLs for THIRTY MINUTES and refuse any repeat, which asks
        # the wrong question entirely: "did we send this recently" instead of "is the console busy
        # with it". Deleting a game and reinstalling it inside half an hour was refused, with a
        # message that claimed the console might still be installing something it had finished, and
        # an instruction to use a Force button the UI does not have. Two jobs, permanently held.
        #
        # The real protection - is there a LIVE bgft job for this content - lives in Queue._run(),
        # which already reads that row. What is left here is a two-minute window that exists only to
        # swallow a genuine double-click, and it is cleared the moment a verdict arrives (about two
        # seconds later), so it can never block a deliberate reinstall.
        now = time.time()
        inflight = getattr(self, "_inflight", None)
        if inflight is None:
            inflight = self._inflight = {}
        for k in [k for k, v in inflight.items() if now - v > 120]:
            inflight.pop(k, None)
        if url in inflight and not force:
            print("[spawn] ignoring a repeat of %s sent %ds ago - the first one has not reported "
                  "back yet" % (url.rsplit("/", 1)[-1], int(now - inflight[url])))
            return False, {"error": "That package was handed to the console a moment ago and it "
                                    "has not answered yet. Give it a few seconds.",
                           "host": "pms-spawn", "busy": True, "do_not_reload": True}
        inflight[url] = now
        # The debounce is for the SUCCESS path only: a hand-over the console accepted and has not
        # reported on yet. Every early return below pops it again, because leaving it set after a
        # refusal meant the queue's own retry-once (2 s later) and a Start pressed on a held job
        # inside two minutes were refused by us - as "console_busy", with the console's real
        # answer replaced by "handed to the console a moment ago". Nothing was in flight.

        # One at a time. The console serialises per-console already, but a retry or a stray
        # request could still overlap - and both installs would share one request file.
        #
        # 20 x 1.5s = 30 seconds, not two minutes: the console releases the latch by itself the
        # moment a verdict exists, so anything still set after half a minute is a spawned installer
        # that died without writing one. Clearing that is exactly what spawn_cleanup() is for, and
        # holding the queue behind it helps nobody.
        waited = 0.0
        for _ in range(20):
            if cancelled and cancelled():
                inflight.pop(url, None)
                return False, {"error": "Canceled before it was handed to the console",
                               "host": "pms-spawn", "canceled": True, "do_not_reload": True}
            st = self.spawn_status()
            if not st.get("busy"):
                break
            time.sleep(1.5)
            waited += 1.5
        else:
            # Only clear a latch that is provably stale. The console releases it by itself once a
            # verdict exists, so one still set with NO result and a short age is guarding a live
            # hand-over - and clearing that is how a second installer gets spawned on top of the
            # first. 600s matches the console's own expiry.
            st = self.spawn_status() or {}
            if st.get("has_result") or int(st.get("busy_for") or 0) >= 600:
                print("[spawn] the lane was still marked busy after %ds but the job is over "
                      "(has_result=%s busy_for=%ss) - clearing it"
                      % (int(waited), st.get("has_result"), st.get("busy_for")))
                self.spawn_cleanup()
            else:
                print("[spawn] the lane is genuinely busy (%ss) - NOT clearing it"
                      % st.get("busy_for"))
                inflight.pop(url, None)
                return False, {"error": "An install is still being handed to the console. Let it "
                                        "finish, then start this one.",
                               "host": "pms-spawn", "busy": True, "do_not_reload": True}
        if cancelled and cancelled():
            inflight.pop(url, None)
            return False, {"error": "Canceled before it was handed to the console",
                           "host": "pms-spawn", "canceled": True, "do_not_reload": True}
        try:
            j = self._shop("/api/engine/install-spawn?uri=%s&name=%s%s"
                           % (quote(url, safe=""), quote(name or "", safe=""),
                              self._pkg_query(pkg)), timeout=90)
        except ShopHTTPError as e:
            # THE CONSOLE ANSWERED. 409 is its busy guard refusing a second install while one is
            # still being handed over - that refusal is protective and must be relayed, never
            # dressed up as an absence. No `queued` key: see the re-drive gate in Queue._run.
            print("[spawn] the console refused with HTTP %d: %s" % (e.status, e.body[:160]))
            inflight.pop(url, None)
            return False, {"error": e.sentence("An install is already running on the console - "
                                               "let it finish, then try this one again"),
                           "host": "pms-spawn", "busy": True, "http_status": e.status,
                           "do_not_reload": True}
        except Exception as e:
            inflight.pop(url, None)
            return False, {"error": "PKG MUTANT SHOP on the %s did not answer - load it again%s"
                                    % (self.kind_label(), self.reload_suffix()),
                           "detail": repr(e)[:200], "host": "pms-spawn",
                           "hint": "is the PKG MUTANT SHOP ELF loaded on the %s?"
                                   % self.kind_label()}
        if not j.get("ok"):
            # This one really is pre-queue: the console answered 200 and said it did not start an
            # installer, so nothing exists to duplicate.
            inflight.pop(url, None)
            return False, {"error": j.get("error") or "The console could not start the installer",
                           "host": "pms-spawn", "queued": False,
                           "busy": bool(j.get("busy"))}
        # The spawned process writes its verdict to a file; poll for it rather than guessing.
        deadline = time.time() + 90
        misses = 0                      # consecutive TRANSPORT failures, not 404s
        while time.time() < deadline:
            time.sleep(2)
            try:
                r = self._shop("/api/engine/spawn-result", timeout=15)
                misses = 0
            except ShopHTTPError:
                # 404 = "no result yet", which is the normal case for most of this loop.
                misses = 0
                continue
            except Exception as e:
                # A TRANSPORT failure is different: the console is not answering. Say so, and stop
                # rather than polling a corpse for the rest of the 90 seconds.
                #
                # But the verdict behind _shop() is CACHED for UP_TTL_DOWN seconds and this loop
                # sleeps 2, so without forcing a fresh probe ONE unlucky connect is re-served from
                # cache twice and spends all three strikes in about four seconds - on an install
                # that is running perfectly. The message that produces is the one that invites a
                # mid-install reload, which is exactly how the duplicate-install crash happened.
                if self.up(fresh=True):
                    misses = 0
                    continue
                misses += 1
                if misses == 1:
                    print("[spawn] the console stopped answering mid-install (%s)" % repr(e)[:90])
                if misses >= 3:
                    print("[spawn] giving up after %d transport failures - the console is gone"
                          % misses)
                    inflight.pop(url, None)
                    return False, {"error": "The console stopped answering while it was installing "
                                            "this package. Check the %s before trying again."
                                            % self.kind_label(),
                                   "host": "pms-spawn", "console_gone": True,
                                   "no_verdict": True, "do_not_reload": True}
                continue
            if r.get("rc") is None:
                continue
            # Clear the handover files either way, so the NEXT install in the queue cannot read
            # this one's request or verdict. This is our equivalent of the third-party host's
            # /cleartmp - except there is no daemon to unstick and no port to clear, because each
            # install is a fresh process that has already exited.
            self.spawn_cleanup()
            self._inflight.pop(url, None)      # it answered; the debounce has done its job
            if r.get("ok"):
                return True, {"res": "0", "host": "pms-spawn", "via": "spawned-process",
                              "content_id": r.get("content_id") or "", "rc": r.get("rc")}
            # A refusal here is pre-BGFT: no task was registered, so nothing is half-queued.
            # The SCE code travels in `rc` and in pms.log (the verdict line in _run prints this
            # whole dict); it is not appended to the sentence any more, because that sentence is
            # the queue row on a television, where a raw hex code is house-style banned.
            # THE CONSOLE'S OWN SENTENCE WINS WHEN IT HAS ONE.
            #
            # install_error_text is a table of PS5 codes. A PS4 answers with codes that are not in
            # it and must not be - 0x80990004 there means something entirely different from
            # anything on that list - so running a PS4 failure through the table produced a PS5
            # sentence about a PS5 problem. Its payload already words the one case worth explaining
            # (a title the Store has a newer version of), and those are the words to show.
            #
            # A no-op for the PS5: its verdict file carries ok / rc / init_rc / via / content_id /
            # pid / uri / token and has never had a `msg`, so the table stays the answer there.
            return False, {"error": (str(r.get("msg") or "").strip()
                                     or install_error_text(r.get("rc"))),
                           "host": "pms-spawn", "rc": r.get("rc"), "queued": False}
        # NO `queued` KEY. Its absence is load-bearing: `queued is False` is the re-drive gate in
        # Queue._run, and re-driving after a timeout is how one package became several BGFT jobs.
        # We do not know what happened - say so, and stop.
        print("[spawn] no verdict after 90s - NOT re-submitting; the console may still be "
              "installing this package")
        # Popped here too: the job is HELD (no_verdict) and only a deliberate Start re-drives it,
        # so this entry could only ever refuse that Start - the live-row guard in _run is what
        # protects the console from a duplicate, not this two-minute memo.
        inflight.pop(url, None)
        return False, {"error": "The console took this package but never reported back. It may "
                                "still be installing it - check the %s before trying again."
                                % self.kind_label(),
                       "host": "pms-spawn", "no_verdict": True, "do_not_reload": True}

    def spawn_cleanup(self):
        """Remove the spawn lane's handover files and release its busy latch. Never raises."""
        try:
            return self._shop("/api/engine/spawn-cleanup", timeout=15)
        except Exception as e:
            print("[spawn] cleanup skipped: %r" % e)
            return None

    def spawn_status(self):
        """Is a spawned install in flight right now?"""
        try:
            return self._shop("/api/engine/spawn-status", timeout=10) or {}
        except Exception:
            return {}

    def install(self, url, name="", force=False, cancelled=None, pkg=None):
        """Install a package on the console with OUR OWN engine. Returns (ok, info).

        This used to dispatch between four lanes. It does not any more - see install_spawn().
        Kept as the single entry point so every caller has one thing to call.

        Historical, for anyone reading old code: 'ezremote' mode = cy33hc's ps5-ezremote-dpi standalone
        payload on TCP 9040: open a socket, send the pkg URL as one line, done. Older 'v2'/'v1'
        modes speak the etaHEN/GoldHEN HTTP+JSON API. Returns (ok, info)."""
        # ONE LANE, and no configuration can select another. There used to be four - two of ours
        # and two third-party protocols - chosen by dpi.mode, whose default here ("ezremote")
        # disagreed with the default in DEFAULT_CONFIG ("v2"), so what an unconfigured PC did was
        # not predictable from reading either one. Both third-party lanes are gone, along with the
        # probe that decided which foreign daemon owned :12800.
        return self.install_spawn(url, name, force=force, cancelled=cancelled, pkg=pkg)

    SMP_CONFIG = "/data/shadowmount/config.ini"

    def register_path(self):
        """Which route will ShadowMount use to REGISTER a newly staged title?

        This is the single setting that decides whether a PS5 backup becomes a working game or a
        tile that crashes, and it is worth stating plainly because it cost a long night to find.

        ShadowMount does two separate things: it MOUNTS the container, then it REGISTERS the title
        with the system. Mounting had always worked. Registering failed every time, with:

            [REG] internal AppInstallTitleDir bridge unavailable
            NOTIFY: Register failed: <game> (<TID>)

        That "internal bridge" is a patch ShadowMount writes into SceShellCore, and on this console
        it is never installed at all — its own startup log says why:

            [SHELLCORE] unexpected prologue: launchApp at 0x1563c660
            [SHELLCORE] lifecycle hooks unavailable; stock behavior kept

        So the direct per-title register route cannot work here. ShadowMount has a second route,
        and its own documentation says this firmware is supposed to be on it already:

            # 1/true/yes/on  -> submit queued installs through sceAppInstUtilAppInstallAll
            # 0/false/no/off -> register each title directly
            # Default: 0 on FW below 12.00, forced to 1 on FW 12.00 and newer

        The console is on 12.70 — but its runtime config dump read `app_install_all=0`, so the
        forcing does not happen in 1.7alpha4 and it used the broken route. Setting the key
        explicitly switched the very same file from "Register failed" to "[REG] Installed", with
        full `appmeta` (which is what gives the PS5 notification the real name and artwork instead
        of "Unknown"). No console restart was involved.

        Returns {"ok", "value", "reason"} and never raises.
        """
        text = self.fs_text(self.SMP_CONFIG, timeout=8)
        if text is None:
            return {"ok": None, "value": None,
                    "reason": "no ShadowMount config at %s" % self.SMP_CONFIG}
        val = None
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("#") or "=" not in s:
                continue
            k, _, v = s.partition("=")
            if k.strip() == "app_install_all":
                val = 1 if v.strip().lower() in ("1", "true", "yes", "on") else 0
        if val == 1:
            return {"ok": True, "value": 1, "reason": "batch registration enabled"}
        return {"ok": False, "value": val,
                "reason": "app_install_all is %s — new titles would mount but fail to register"
                          % ("absent" if val is None else "0")}

    def ensure_register_path(self):
        """Put ShadowMount on the register route that works here, rewriting its config if needed.

        Returns 'ok' | 'fixed' | 'unknown'. 'fixed' means the file was changed and ShadowMount has
        to be restarted before it takes effect — it reads the config once, at startup.

        Deliberately conservative: the file is only rewritten after it has been read in full, the
        original text is preserved byte for byte, and the key is appended rather than reformatting
        anything. Everything else in that config is the user's (fakelib excludes, kstuff rules,
        scan paths) and must survive untouched.
        """
        st = self.register_path()
        if st.get("ok") is True:
            return "ok"
        if st.get("ok") is None:
            return "unknown"
        try:
            text = self.fs_text(self.SMP_CONFIG, timeout=8)
            if not text or not text.strip():
                return "unknown"                      # never overwrite a config we failed to read
            out, done = [], False
            for line in text.splitlines(True):
                s = line.strip()
                if not s.startswith("#") and s.split("=", 1)[0].strip() == "app_install_all":
                    if not done:
                        out.append("app_install_all=1\n")
                        done = True
                    continue                          # drop any further/duplicate assignments
                out.append(line)
            if not done:
                out.append(
                    "\n# --- REGISTER PATH (set by PKG MUTANT SHOP) ------------------------------\n"
                    "# FW 12.70. The direct per-title register route needs a SceShellCore patch that\n"
                    "# is never installed on this console (\"unexpected prologue: launchApp\" ->\n"
                    "# \"lifecycle hooks unavailable\"), so it always ended in\n"
                    "# \"[REG] internal AppInstallTitleDir bridge unavailable\" / Register failed.\n"
                    "# This routes registration through sceAppInstUtilAppInstallAll instead, which\n"
                    "# ShadowMount's own docs say is required on FW 12.00 and newer.\n"
                    "app_install_all=1\n")
            new = "".join(out)
            if not self.fs_write(self.SMP_CONFIG, new.encode("utf-8"), timeout=20):
                return "unknown"
            return "fixed"
        except Exception:
            return "unknown"

    def restart_shadowmount(self, wait=45):
        """Reload the ShadowMount payload so a config change takes effect. Only when it is safe.

        Restarting remounts every backup, so it must never happen while a game is running off one.
        Returns 'restarted' | 'busy' | 'failed'.
        """
        try:
            # running_title() returns the CheatRunner-shaped {"titleId": ...}; reading "title_id"
            # here made this guard permanently dead, so ShadowMount could be restarted - and every
            # backup remounted - underneath a game running off one.
            if (self.running_title() or {}).get("titleId"):
                return "busy"
        except Exception:
            pass
        pm = self.pldmgr
        path = pm.payload_path("shadowmount")
        if not path:
            return "failed"
        for pid in pm.pids_named("shadowmountplus"):
            pm.kill(pid)
        time.sleep(5)
        if not pm.load(path):
            return "failed"
        deadline = time.time() + wait
        while time.time() < deadline:
            if pm.pids_named("shadowmountplus"):
                time.sleep(8)              # let it finish its startup scan before we stage anything
                return "restarted"
            time.sleep(3)
        return "failed"

    def title_has_mount_link(self, title_id):
        """Does `/user/app/<TID>/mount.lnk` exist — i.e. is a container actually mounted for it?"""
        if not self.ip or not title_id:
            return False
        # fs_list returns None rather than raising, so the try/finally that used to tear down an
        # FTP connection here has nothing left to guard.
        for r in ("/user/app", "/mnt/ext0/user/app", "/mnt/ext1/user/app", "/mnt/ext2/user/app"):
            rows = self.fs_list("%s/%s" % (r, title_id), timeout=10)
            if not rows:
                continue
            for e in rows:
                if (e.get("name") or "").strip().lower() == "mount.lnk":
                    return True
        return False

    def title_is_mounted(self, title_id):
        """Has ShadowMount finished — mounted AND *registered* the title?

        BOTH are required, and each one alone has already lied to us in production:

          * `mount.lnk` alone — ShadowMount writes the links BEFORE it registers, so a title whose
            register step failed still has one. That reported "ready to play" for Evergate while
            the console had never registered it.

          * app.db alone — a registration outlives its data. Little Nightmares III sat in app.db at
            75 MB of metadata with no container on the console at all: `/user/app/PPSA05144` held
            only `icon0.png` and `sce_sys`, no `mount.lnk`. Waiting on app.db would have passed
            instantly, on a tile that crashes.

        A genuinely working backup has both — verified against all 17 mounted titles, and against
        each of the two failures above. So we require both.
        """
        if not self.ip or not title_id:
            return False
        try:
            if title_id not in (self.installed_titles(force=True) or []):
                return False
            return self.title_has_mount_link(title_id)
        except Exception:
            return False

    def installed_app_pkg(self, title_id):
        """Size of the installed game's `app.pkg` on the console, or None if it is not there.

        This is the only honest proof that a title really installed. app.db registers a title as
        soon as its metadata lands, and `sceAppInstUtilAppInstallPkg` writes metadata WITHOUT the
        data — so "it is in app.db" has been telling us a game was ready when the console had
        nothing to run. A registered title with no `app.pkg` is the broken tile that crashes on
        launch. Verified layout: /user/app/<TID>/app.pkg, or /mnt/ext<n>/user/app/<TID>/app.pkg
        when it lives on extended storage.
        """
        if not self.ip or not title_id:
            return None
        roots = ["/user/app", "/mnt/ext0/user/app", "/mnt/ext1/user/app", "/mnt/ext2/user/app"]
        try:
            for r in roots:
                rows = self.fs_list("%s/%s" % (r, title_id), timeout=10)
                if not rows:
                    continue
                for e in rows:
                    name, size = (e.get("name") or ""), e.get("size")
                    if name.strip().lower() == "app.pkg":
                        try:
                            return int(size)
                        except (TypeError, ValueError):
                            return None
            return None
        except Exception:
            return None

    def install_progress(self, title_id):
        """Live on-console install byte-progress from bgft.db (Background File Transfer):
        returns (transferred_total, length_total) for the title's newest row, or None.
        Best-effort - some install paths don't refresh bgft, so the worker also confirms
        completion via app.db (installed_titles)."""
        if not self.ip or not title_id:
            return None
        # Fetched through _pull_db, which prefers OUR on-console file API and only falls
        # back to the third-party FTP. Unique temp name per pull: the confirm loop, the
        # library scan and the UI can all be here at once, and one thread truncating a file
        # another is mid-read on produces "database disk image is malformed" - which the
        # caller reads as "not installed yet". sweep_temp_dbs() still matches the pms_ prefix.
        tmp = self._pull_db("/system_data/priv/mms/bgft.db")
        if not tmp:
            return None
        try:
            con = sqlite3.connect(tmp)
            row = con.execute(
                "SELECT transferred_total,length_total FROM tbl_downloads "
                "WHERE title_id=? ORDER BY last_updated DESC LIMIT 1", (title_id,)).fetchone()
            con.close()
            if row and row[1]:
                return int(row[0] or 0), int(row[1])
        except sqlite3.Error:
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return None

    # ---- console filesystem: ours first, somebody else's FTP second --------------------------
    #
    # Every console read in this file went through FTP, and FTP here belongs to etaHEN (:1337) or
    # to ftpsrv (:2121) - neither of which we ship. That was 18 call sites of dependency on other
    # people's software for the sake of reading files on a machine where our own payload already
    # runs as root with an HTTP server.
    #
    # So: try our own ELF first, fall back to FTP, and remember which answered. Nothing regresses
    # if the ELF is not loaded - the old path is still there, unchanged, underneath.
    #
    # It is also simply faster. Measured on this console, bgft.db (94,208 bytes), the file the
    # install-confirm loop re-reads every few seconds: HTTP 0.011 s vs FTP 0.079 s - 7.3x - because
    # FTP pays a connect, a login and a data-channel setup for every single fetch.
    FS_TTL = 20.0                       # how long a "the ELF is not answering" verdict is trusted

    UP_TTL_OK = 4.0            # a console that answered is re-checked often - it can be switched off
    UP_TTL_DOWN = 8.0          # a console that did not is given another chance quickly

    def up(self, budget=0.6, fresh=False):
        """Is the console's shop port accepting connections RIGHT NOW? Cached briefly.

        A TCP connect to a machine that is off is refused or times out in milliseconds on a LAN,
        while an HTTP request to the same address waits out its whole timeout. With the PS5 off
        that difference was 10 s on /api/health and 20 s on /api/library - the UI polls health, so
        the entire app sat grey. This only ever skips calls that were going to fail."""
        now = time.time()
        cached, at = getattr(self, "_up", None), getattr(self, "_up_at", 0.0)
        if not fresh and cached is not None and now - at < (self.UP_TTL_OK if cached else self.UP_TTL_DOWN):
            return cached
        if not self.ip:
            return False
        port = self.cfg.get("console", {}).get("shop_port", 8710)
        sock = socket.socket()
        sock.settimeout(budget)
        try:
            ok = sock.connect_ex((self.ip, int(port))) == 0
        except Exception:
            ok = False
        finally:
            try:
                sock.close()
            except Exception:
                pass
        self._up, self._up_at = ok, now
        return ok

    def _fs_http_ok(self):
        """Has our own on-console server answered recently? Cached, so a console without the ELF
        loaded does not pay a probe on every read."""
        now = time.time()
        # A closed port is a definitive no, and costs milliseconds rather than a whole timeout.
        if not self.up():
            return False
        if self._fs_ok_at and now - self._fs_ok_at < self.FS_TTL:
            return self._fs_ok
        return None                     # unknown - just try it

    def _fs_url(self, op, path):
        port = self.cfg.get("console", {}).get("shop_port", 8710)
        return "http://%s:%d/api/fs/%s?path=%s" % (self.ip, port, op, quote(path, safe=""))

    def fs_read(self, path, timeout=60):
        """Bytes of a file on the console, or None. Our ELF first, FTP second."""
        if not self.ip:
            return None
        if self._fs_http_ok() is not False:
            try:
                with urllib.request.urlopen(self._fs_url("read", path), timeout=timeout) as r:
                    data = r.read()
                self._fs_ok, self._fs_ok_at = True, time.time()
                return data
            except urllib.error.HTTPError:
                # The server answered and said no (missing file). That is a real answer about the
                # file, not a transport failure, so do NOT fall through to FTP for it.
                self._fs_ok, self._fs_ok_at = True, time.time()
                return None
            except Exception:
                self._fs_ok, self._fs_ok_at = False, time.time()
        try:
            ftp = self._ftp(8)
            buf = io.BytesIO()
            ftp.retrbinary("RETR " + path, buf.write)
            try:
                ftp.quit()
            except Exception:
                pass
            return buf.getvalue()
        except Exception:
            return None

    def fs_list(self, path, timeout=20):
        """[{name, dir, size, mtime}, ...] for a console directory, or None.

        The FTP fallback parses a LIST listing, which is why the HTTP form exists at all: LIST
        output is a formatted human string with no schema, and every server words it differently.
        """
        if not self.ip:
            return None
        if self._fs_http_ok() is not False:
            try:
                with urllib.request.urlopen(self._fs_url("list", path), timeout=timeout) as r:
                    j = json.loads(r.read().decode("utf-8", "replace"))
                self._fs_ok, self._fs_ok_at = True, time.time()
                if j.get("ok"):
                    # Carry the console's own "this is not the whole directory" flag with the
                    # entries. Callers that only look for one name (app.pkg, mount.lnk) are fine
                    # with a partial page; a caller computing a difference is not.
                    out = _FsListing(j.get("entries") or [])
                    out.truncated = bool(j.get("truncated"))
                    return out
                return None
            except urllib.error.HTTPError:
                self._fs_ok, self._fs_ok_at = True, time.time()
                return None
            except Exception:
                self._fs_ok, self._fs_ok_at = False, time.time()
        return self._fs_list_ftp(path)

    def _fs_list_ftp(self, path):
        """The FTP LIST half of fs_list(), on its own so a reader that was handed a TRUNCATED
        HTTP page can ask for the complete listing this way. FTP has no size cap.

        Gated by a 1.2 s connect probe first: with no FTP up, _ftp() alone burns two full
        8 s connect timeouts, and the cheat status asks for four directories in a row."""
        if not self.ip or not self.ftp_ok(timeout=1.2):
            return None
        try:
            ftp = self._ftp(8)
            lines = []
            ftp.retrlines("LIST " + path, lines.append)
            try:
                ftp.quit()
            except Exception:
                pass
            out = []
            for ln in lines:
                parts = ln.split(None, 8)
                if len(parts) < 9:
                    continue
                name = parts[8]
                if name in (".", ".."):
                    continue
                try:
                    size = int(parts[4])
                except ValueError:
                    size = 0
                out.append({"name": name, "dir": ln[:1] == "d", "size": size, "mtime": 0})
            return _FsListing(out)
        except Exception:
            return None

    def fs_list_complete(self, path, timeout=20):
        """A listing that is guaranteed whole, or None when one cannot be had.

        HTTP first; if the console says it cut the page short, the FTP LIST is the only complete
        answer available, and when there is no FTP the honest result is "unknown" - never the
        partial page."""
        rows = self.fs_list(path, timeout=timeout)
        if rows is None or not getattr(rows, "truncated", False):
            return rows
        full = self._fs_list_ftp(path)
        if full is None:
            print("[fs] %s: the console's listing was cut short and no FTP is up to read the "
                  "rest - treating it as unknown" % path)
        return full

    def fs_write(self, path, data, timeout=900, size=None):
        """Write to a console file. Our ELF writes to <path>.part and renames, so a broken transfer
        can never be seen as a finished file. Returns True on success.

        `data` is bytes, or a file-like object with `size` given — in which case urllib streams it
        straight off the disk instead of loading it into memory, which is the difference between
        this being usable for a 90 GB game and only for small ones.
        """
        if not self.ip:
            return False
        blen = int(size) if size is not None else len(data)
        if self._fs_http_ok() is not False:
            try:
                req = urllib.request.Request(
                    self._fs_url("write", path), data=data, method="POST",
                    headers={"Content-Type": "application/octet-stream",
                             "Content-Length": str(blen)})
                with urllib.request.urlopen(req, timeout=timeout) as r:
                    j = json.loads(r.read().decode("utf-8", "replace"))
                self._fs_ok, self._fs_ok_at = True, time.time()
                if j.get("ok"):
                    return True
            except _Cancelled:
                # The caller's progress reader stopped the transfer on purpose. Do NOT retry it
                # over FTP, and do NOT mark our own transport dead - neither is broken. The ELF
                # unlinks its .part when the connection drops mid-body, so nothing is left behind.
                raise
            except urllib.error.HTTPError:
                self._fs_ok, self._fs_ok_at = True, time.time()
                return False
            except Exception:
                self._fs_ok, self._fs_ok_at = False, time.time()
        ftp = None
        try:
            ftp = self._ftp(15)
            ftp.timeout = timeout
            try:
                ftp.sock.settimeout(timeout)      # a multi-GB push stalls while the console flushes
            except Exception:
                pass
            for d in _mkdirs_list(os.path.dirname(path).replace("\\", "/")):
                try:
                    ftp.mkd(d)
                except Exception:
                    pass
            # .part then rename, matching what the ELF's own write does. Writing straight to the
            # final name means an interrupted transfer leaves a partial container in ShadowMount's
            # scan folder, where it looks exactly like a real game - half a game that mounts, or
            # refuses to, for no visible reason.
            part = path + ".part"
            ftp.storbinary("STOR " + part,
                           io.BytesIO(data) if isinstance(data, bytes) else data,
                           blocksize=262144)
            try:
                ftp.delete(path)                  # a stale copy would block the rename
            except Exception:
                pass
            ftp.rename(part, path)
            try:
                ftp.quit()
            except Exception:
                pass
            return True
        except _Cancelled:
            try:
                ftp.delete(path + ".part")      # never leave a partial in a watched folder
            except Exception:
                pass
            raise
        except Exception:
            # A dropped link or a refused STOR leaves the same .part behind as a cancel does, and
            # only the cancel branch used to remove it - a multi-GB orphan in a ShadowMount watch
            # folder, invisible to the UI (console_backups() filters by extension) and eating the
            # drive. Best effort over the same connection, if there still is one.
            if ftp is not None:
                try:
                    ftp.delete(path + ".part")
                except Exception:
                    pass
            return False

    def fs_text(self, path, timeout=20):
        """A console text file as str, or None. Convenience over fs_read - most of these are small
        configs and json records, and every caller was decoding the same way."""
        data = self.fs_read(path, timeout=timeout)
        if data is None:
            return None
        return data.decode("utf-8", "replace")

    def fs_mkdir(self, path, timeout=15):
        """Create a directory on the console (parents included). True on success.

        Our ELF's write path calls mkparents() itself, so this is only needed where a directory has
        to exist independently of any file - the cheat tree, and the mount destination.
        """
        if not self.ip:
            return False
        if self._fs_http_ok() is not False:
            try:
                with urllib.request.urlopen(self._fs_url("mkdir", path), timeout=timeout) as r:
                    j = json.loads(r.read().decode("utf-8", "replace"))
                self._fs_ok, self._fs_ok_at = True, time.time()
                return bool(j.get("ok"))
            except urllib.error.HTTPError:
                self._fs_ok, self._fs_ok_at = True, time.time()
                return False
            except Exception:
                self._fs_ok, self._fs_ok_at = False, time.time()
        try:
            ftp = self._ftp(8)
            for d in _mkdirs_list(path):
                try:
                    ftp.mkd(d)
                except Exception:
                    pass                        # already there
            try:
                ftp.quit()
            except Exception:
                pass
            return True
        except Exception:
            return False

    def _pull_db(self, remote):
        """Fetch a console sqlite database to a private temp file. Returns the path, or None.

        Every caller used to open its own FTP connection for this. Now they share one transport
        decision, and all four became independent of etaHEN's FTP at the same time.
        """
        data = self.fs_read(remote, timeout=60)
        if not data:
            return None
        _fd, tmp = tempfile.mkstemp(prefix="pms_db_", suffix=".db")
        try:
            with os.fdopen(_fd, "wb") as f:
                f.write(data)
        except OSError:
            self._drop_tmp(tmp)
            return None
        return tmp

    def _db_stamp(self, remote):
        """(size, mtime) of a file on the console, or None when it cannot be asked.

        READ-ONLY. /api/fs/stat is a stat() on the console - fs_send_stat in both ELFs - so it
        creates, starts, resumes and cancels nothing, and answers with one small JSON object.
        None means "do not know", and a caller that gets None must transfer the file rather than
        conclude anything from the silence.

        Deliberately does NOT touch self._fs_ok. A stat that fails is usually a path that is not
        there, not an ELF that has stopped answering, and the install preflight reads that flag to
        decide which transport to use.
        """
        if not self.ip or self._fs_http_ok() is False:
            return None
        try:
            with urllib.request.urlopen(self._fs_url("stat", remote), timeout=6) as r:
                j = json.loads(r.read().decode("utf-8", "replace"))
        except Exception:
            return None
        if not isinstance(j, dict) or not j.get("ok"):
            return None
        try:
            sz, mt = int(j.get("size") or 0), int(j.get("mtime") or 0)
        except (TypeError, ValueError):
            return None
        # Zero is what the FTP-backed listing reports when it does not really know, and "I do not
        # know" is not an answer anything may skip a multi-megabyte transfer on.
        if sz <= 0 or mt <= 0:
            return None
        return (sz, mt)

    def _pull_db_cached(self, remote, force=False):
        """_pull_db(), except the bytes are reused when the console says the file has not moved.

        app.db was pulled WHOLE on every cold library refresh - the entire database, over Wi-Fi,
        off the console's single accept loop, for as long as the app is open - and between
        installs it is byte for byte the same file. One stat answers whether it changed, so ask
        first and transfer only when the answer moved.

        `force` PULLS UNCONDITIONALLY and caches nothing, and that is not optional:
        installed_titles(force=True) is the install-confirm loop asking whether a title has really
        landed, and a cached answer there is exactly the lie that rule exists to prevent.

        Only app.db comes through here. bgft.db and addcont.db - the two databases read WHILE an
        install is running - keep calling _pull_db() every single time.
        """
        cache = getattr(self, "_db_bytes_cache", None)
        if cache is None:
            cache = self._db_bytes_cache = {}
        if force:
            cache.pop(remote, None)
        stamp = None if force else self._db_stamp(remote)
        held = cache.get(remote)
        if stamp is not None and held is not None and held[0] == stamp:
            data = held[1]
        else:
            data = self.fs_read(remote, timeout=60)
            if not data:
                return None
            # Held in this PC's memory, one copy per console. Capped, because an unbounded "keep
            # the last file we read" is a memory leak wearing a cache's clothes; past the cap the
            # transfer simply happens every time, which is what it did before.
            if stamp is not None and len(data) <= (32 << 20):
                cache[remote] = (stamp, data)
            else:
                cache.pop(remote, None)
        _fd, tmp = tempfile.mkstemp(prefix="pms_db_", suffix=".db")
        try:
            with os.fdopen(_fd, "wb") as f:
                f.write(data)
        except OSError:
            self._drop_tmp(tmp)
            return None
        return tmp

    @staticmethod
    def _drop_tmp(path):
        # None-tolerant on purpose: callers reach here on failure paths where the temp file was
        # never created, and os.remove(None) raises TypeError, which `except OSError` misses.
        if not path:
            return
        try:
            os.remove(path)
        except OSError:
            pass

    def install_job_row(self, title_id):
        """Newest bgft.db row for a title as (status, transferred, length, last_updated).

        `install_progress` deliberately returns only bytes, which cannot distinguish "this title
        has an old completed row from its base install" from "the thing I just submitted landed".
        The status code is what separates them:

            1000  queued / parked        1009  downloading or installing
            1026  a PATCH/update install completed
            1036  a full title install completed

        A base game leaves a 1036 row; an update leaves its own 1026 row. That distinction is the
        only honest proof an ADD-ON installed, because the base game's app.pkg is already on disk
        and trivially satisfies any size check made against the add-on's much smaller package.
        """
        if not self.ip or not title_id:
            return None
        # Fetched through _pull_db, which prefers OUR on-console file API and only falls
        # back to the third-party FTP. Unique temp name per pull: the confirm loop, the
        # library scan and the UI can all be here at once, and one thread truncating a file
        # another is mid-read on produces "database disk image is malformed" - which the
        # caller reads as "not installed yet". sweep_temp_dbs() still matches the pms_ prefix.
        tmp = self._pull_db("/system_data/priv/mms/bgft.db")
        if not tmp:
            return None
        try:
            con = sqlite3.connect(tmp)
            row = con.execute(
                "SELECT status,transferred_total,length_total,last_updated FROM tbl_downloads "
                "WHERE title_id=? ORDER BY last_updated DESC, rowid DESC LIMIT 1",
                (title_id,)).fetchone()
            con.close()
            if row:
                # Column types are not guaranteed here: on this firmware last_updated is a TEXT
                # timestamp, and a numeric column can still come back as text. Coerce what has to be
                # a number, and keep last_updated verbatim — it is only ever compared for equality
                # against the pre-handoff snapshot, never arithmetic.
                def _n(v):
                    try:
                        return int(v)
                    except (TypeError, ValueError):
                        return 0
                return (_n(row[0]), _n(row[1]), _n(row[2]), row[3])
        except sqlite3.Error:
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        return None

    def installed_addons(self, force=False):
        """content_ids of every DLC the console has registered, from addcont.db. None if unreadable.

        This is the only place a DLC is recorded — a DLC does not appear in app.db and does not get
        its own app.pkg, so without this we have no way to know one is already there.
        """
        now = time.time()
        if not force and self._addons is not None and now - self._addons_ts < 30:
            return self._addons
        if not self.ip:
            return None
        # Fetched through _pull_db, which prefers OUR on-console file API and only falls
        # back to the third-party FTP. Unique temp name per pull: the confirm loop, the
        # library scan and the UI can all be here at once, and one thread truncating a file
        # another is mid-read on produces "database disk image is malformed" - which the
        # caller reads as "not installed yet". sweep_temp_dbs() still matches the pms_ prefix.
        tmp = self._pull_db("/system_data/priv/mms/addcont.db")
        if not tmp:
            return None
        try:
            con = sqlite3.connect(tmp)
            got = {str(r[0]) for r in con.execute("SELECT content_id FROM addcont") if r[0]}
            con.close()
        except sqlite3.Error:
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        self._addons, self._addons_ts = got, now
        return got

    def _name_of(self, tid):
        """The console's own name for a title, falling back to the id. Used in messages the user
        has to make a decision about - "Riptide GP2 is already installed" is a question someone can
        answer; "CUSA02365 is already installed" is one they have to go and look up."""
        try:
            for a in (self.console_apps() or []):
                if a.get("title_id") == tid and a.get("name"):
                    return a["name"]
        except Exception:
            pass
        return tid

    def already_installed(self, kind, title_id, version="", content_id="", pkg_bytes=0):
        """Is this exact package already on the console? Returns a reason string, or "".

        Reinstalling something that is already there is never harmless: for a base game the console
        SKIPS the re-pull and keeps the old files (the stale_install trap), and for an add-on it is
        pure wasted transfer. Each kind needs a different proof, because the console records them
        in completely different places:

            base game   app.db registration AND its own app.pkg on disk at full size
            update      app.db's APP_VER already at or past the package's version
            dlc         its content_id present in addcont.db
            backup      registered AND a mount.lnk on disk (title_is_mounted) - a mounted
                        ShadowMount title has NO app.pkg, so the base-game proof can never see
                        it, and a repeat Install re-sent up to 100 GB onto a mounted container

        Anything we cannot read comes back "" — never block an install on a failed lookup.
        """
        kind = (kind or "").lower()
        tid = (title_id or "").upper()
        if not tid:
            return ""
        try:
            if kind == "backup":
                if self.title_is_mounted(tid):
                    return "%s is already mounted on %s" % (self._name_of(tid), self.name)
                return ""
            if kind in ("dlc", "backport"):
                have = self.installed_addons()
                cid = (content_id or "").strip()
                if have is not None and cid and cid in have:
                    return "this add-on is already installed on %s" % self.name
                return ""
            if kind in ("update", "patch"):
                want = _ver_key(version)
                if not want:
                    return ""
                for a in (self.console_apps() or []):
                    if str(a.get("title_id", "")).upper() != tid:
                        continue
                    have = _ver_key(a.get("app_ver") or "")
                    if have and have >= want:
                        return ("%s is already at version %s" % (a.get("name") or tid,
                                                                 a.get("app_ver")))
                return ""
            # base game
            if tid not in (self.installed_titles() or []):
                return ""
            got = self.installed_app_pkg(tid)
            if got and (not pkg_bytes or got >= int(pkg_bytes * 0.98)):
                return ("%s is already installed on %s" % (self._name_of(tid), self.name))
        except Exception:
            return ""
        return ""

    # Elf Arsenal and everything it spawns. NOT the generic "payload.elf" - Payload Manager names
    # EVERY loaded payload that, including our own ELF, so killing by that name kills the app
    # itself (learned the hard way). Only ever match specific names.
    # Elf Arsenal and the daemons Arsenal itself spawns. Used both to answer "is Arsenal here?"
    # and to decide what to sweep off the install port.
    #
    # nanodns.elf, ftpsrv.elf and klogsrv were in this tuple and are NOT Arsenal's. They are
    # Payload Manager autoload entries - /data/pldmgr/autoload.txt on this console reads
    # "!5000 / nanodns.elf / ftpsrv.elf / kstuff-lite_v1.10.elf / shadowmountplus.elf" - so they
    # come back on every boot no matter what we do. CHANGELOG.md already recorded that correction
    # once; it was never propagated here, and the consequences were both live:
    #   * "Fix install-port conflicts" stopped ftpsrv.elf and nanodns.elf - the only two matches on
    #     a healthy console - and nothing that can contend for :12800. ftpsrv owns :2121, which is
    #     the FTP this app itself uses to read app.db and bgft.db.
    #   * _arsenal_running() returned True on a console with no Arsenal, so every install ran the
    #     full Arsenal preflight: an FTP fetch of /data/elf-arsenal/config.ini plus two refused
    #     connects to :6969, measured at ~4.1 s of dead time per install.
    # The fight is over :12800. ftpsrv binds 2121, nanodns binds 53/udp, klogsrv binds neither -
    # none of them can ever be an install-port conflict, so removing them loses nothing.
    #
    # NEVER add the bare "payload" here: Payload Manager names EVERY loaded payload payload.elf,
    # our own ELF included, and sweeping by that name has already killed this app once.
    # The Arsenal/conflict process lists lived here. They named elf-arsenal.elf, dpiv2.elf,
    # dpi.elf, tile-autoinst and garlic-savemgr so the app could stop them fighting over the
    # install port. This app has no install port to fight over and does not ship either payload,
    # so deciding which of the user's processes to kill is no longer its business.

    # ---- cheat / patch library -----------------------------------------------------------------
    # The ELF embeds the whole library and writes it to the console at boot (see the CHEATS_DIR
    # note at the top of this file); this sync is the REPAIR path, from a source checkout, for a
    # console whose /data was wiped. Pushing is INCREMENTAL - list what the console already has and
    # send only the difference - so a healthy console costs a few listings and nothing is sent.
    #
    # "What the console has" MUST come from a COMPLETE listing. The console's /api/fs/list stops
    # at 96000 bytes and flags it; three of the four folders are past that size, so the HTTP page
    # alone counted ~2092 files as missing on every pass and re-sent them every 15 minutes. A
    # folder whose full listing cannot be had is reported as unknown and nothing is sent to it.
    def cheat_library_status(self):
        """{sub: {"local": n, "console": n|None, "missing": n|None}} plus totals. Never raises.

        `console`/`missing` are None for a folder that could not be listed in full."""
        out, tl, tc, tm = {}, 0, 0, 0
        # Reachable means the console is answering on SOME transport we can list with. It used
        # to be `bool(self.ip)`, which reported reachable:true and every file missing while the
        # PS5 was switched off.
        reachable = bool(self.ip) and (self.up() or self.ftp_ok(timeout=0.8))
        unknown = 0
        try:
            # The root's own (tiny) listing, read once: it is what separates "that folder does
            # not exist yet" from "it exists and could not be read in full".
            parent = self.fs_list(CONSOLE_CHEAT_ROOT, timeout=10) if reachable else None
            present = {e.get("name") for e in (parent or []) if e.get("dir")}
            for sub in CHEAT_SUBDIRS:
                local = _cheat_local_files(sub)
                remote = None
                if reachable:
                    rows = self.fs_list_complete("%s/%s" % (CONSOLE_CHEAT_ROOT, sub), timeout=15)
                    # None = the directory does not exist yet ("nothing there") OR its listing was
                    # cut short with no way to read the rest. Only the first means "everything is
                    # missing".
                    if rows is not None:
                        remote = {e.get("name") for e in rows}
                    elif sub not in present:
                        remote = set()
                    else:
                        unknown += 1
                miss = len(local - remote) if remote is not None else None
                out[sub] = {"local": len(local), "console": (len(remote) if remote is not None else None),
                            "missing": miss}
                tl += len(local)
                if remote is not None:
                    tc += len(remote); tm += miss
        except Exception:
            reachable = False
        return {"ok": True, "reachable": reachable, "dirs": out, "local_total": tl,
                "console_total": (tc if reachable else None), "missing_total": (tm if reachable else None),
                "unknown_dirs": unknown}

    def sync_cheat_library(self, log=None, budget=None):
        """Push every cheat file the console does not already have. Returns a summary dict."""
        def say(m):
            if log:
                log(m)
        # THE PS4 READS THESE NOW. This used to refuse, and the reason it gave was correct at the
        # time: the PS4's payload answered every cheat route with "not available yet", so pushing
        # up to 7022 files one FTP upload at a time filled a folder nothing would ever read.
        #
        # What changed is the engine, not the storage. The PS4 payload carries the PS5's cheat
        # engine now (ps4-app/onconsole/cheat_core.h, generated from ps5-app/onconsole/server.c)
        # and reads its library from exactly this folder. Unlike the PS5, whose ELF ships the whole
        # library inside it, the PS4's does not - its payload is loaded into a system daemon and is
        # kept small deliberately - so this sync is the ONLY way its library gets there.
        if not os.path.isdir(CHEATS_DIR):
            # THIS IS NO LONGER "BY DESIGN". The exe ships assets/cheats-library.zip and expands it
            # here on a background thread at startup, so a missing folder in a frozen build means the
            # expansion has not finished yet, or it failed. The old message said this build did not
            # carry a library at all, which sends whoever reads it somewhere else entirely.
            frozen = getattr(sys, "frozen", False)
            return {"ok": False, "error":
                    ("The cheat library has not finished unpacking yet. It expands to %s the first "
                     "time this app runs - give it a moment and try again. If it never appears, the "
                     "app could not write beside its own file." % CHEATS_DIR) if frozen else
                    ("no bundled cheat library at %s" % CHEATS_DIR)}
        if not self.ip:
            return {"ok": False, "error": "no console configured"}
        if not _CHEAT_SYNC_LOCK.acquire(blocking=False):
            return {"ok": False, "busy": True,
                    "error": "A cheat sync is already running - let it finish first"}
        try:
            return self._sync_cheat_library_locked(say, budget)
        finally:
            _CHEAT_SYNC_LOCK.release()

    def _sync_cheat_library_locked(self, say, budget):
        sent = failed = skipped = 0
        unknown = []
        started = time.time()
        try:
            for d in ("/data/pkg-mutant-shop", CONSOLE_CHEAT_ROOT):
                self.fs_mkdir(d)
            for sub in CHEAT_SUBDIRS:
                ldir = os.path.join(CHEATS_DIR, sub)
                if not os.path.isdir(ldir):
                    continue
                rdir = "%s/%s" % (CONSOLE_CHEAT_ROOT, sub)
                self.fs_mkdir(rdir)
                rows = self.fs_list_complete(rdir, timeout=20)
                if rows is None:
                    # The folder exists (fs_mkdir just made sure) and could not be listed in full:
                    # a difference against a partial page would re-send files the console already
                    # holds, so this folder gets nothing this pass.
                    say("cheats: %s - could not read the whole folder, sending nothing to it" % sub)
                    unknown.append(sub)
                    continue
                have = {e.get("name") for e in rows}
                todo = sorted(_cheat_local_files(sub) - have)
                skipped += len(have)
                if todo:
                    say("cheats: %s — sending %d file(s)" % (sub, len(todo)))
                for i, name in enumerate(todo):
                    if budget and time.time() - started > budget:
                        say("cheats: time budget reached, will continue next run")
                        return {"ok": True, "sent": sent, "failed": failed, "skipped": skipped,
                                "partial": True, "unknown_dirs": unknown,
                                "seconds": round(time.time() - started, 1)}
                    try:
                        lp = os.path.join(ldir, name)
                        with open(lp, "rb") as fh:
                            ok = self.fs_write("%s/%s" % (rdir, name), fh,
                                               timeout=60, size=os.path.getsize(lp))
                        if ok:
                            sent += 1
                        else:
                            failed += 1
                    except Exception:
                        failed += 1
                    if sent and sent % 400 == 0:
                        say("cheats: %d sent…" % sent)
        except Exception as e:
            return {"ok": False, "error": "cheat sync failed (%r)" % (e,),
                    "sent": sent, "failed": failed, "skipped": skipped, "unknown_dirs": unknown}
        return {"ok": True, "sent": sent, "failed": failed, "skipped": skipped, "partial": False,
                "unknown_dirs": unknown, "seconds": round(time.time() - started, 1)}

    def invalidate_apps(self):
        """Drop the installed-titles cache. Call this the moment something finishes installing:
        the cache is 30s, which is why a freshly applied patch kept reading as still pending."""
        self._apps = None
        self._apps_ts = 0.0
        # The backup-folder scan has its own 30 s memo, and console_apps() folds it in: without
        # dropping it too, a backup that just mounted (or was just removed) kept its old size,
        # path and format on the next read for up to half a minute.
        self._backups = None
        self._backups_ts = 0.0
        # The USB-stick listing is cached too now (console_usb_packages); a stick's package that
        # just installed should stop being offered as installable on the next library read.
        self._usbpkg_memo = None
        # AND THE app.db BYTES. console_apps() now reuses the file it already has when the console
        # says the size and mtime have not moved, and mtime has one-second granularity - so an
        # install that finished inside the same second as the last pull, without changing the
        # file's length, could otherwise be read back from the copy taken just before it. This
        # method is called the moment something lands on the console, which is exactly where that
        # doubt is cheapest to remove.
        self._db_bytes_cache = {}

    # ------------------------------------------------ the PS4's dashboard app
    #
    # A PS5 gets its tile from the ELF, which installs the package it carries. The PS4 cannot work
    # that way: its tile package CARRIES the payload (so that pressing the icon can start the shop
    # with this PC switched off), and a payload that also carried the tile would contain a copy of
    # itself - every rebuild bigger than the last. So the PC installs it, through the ordinary
    # install lane, which is the same lane the tile would have used anyway.
    def ps4_tile_installed(self, force=False):
        """True / False / None (could not ask). Proof is the app's own app.pkg, as for any title.

        NONE IS NOT FALSE. installed_app_pkg answers None both for "the console says it is not
        there" and for "the console did not answer", and collapsing those to False made a console
        that was merely switched off report the app as missing - which the settings panel would
        then offer to install, and the install would fail. Ask whether the console is there first;
        only a console that can answer gets to say no."""
        if not self.is_ps4():
            return None
        try:
            if not self._reachable_fast():
                return None
            got = self.installed_app_pkg(PS4_TILE_TID)
            return bool(got) if got is not None else None
        except Exception:
            return None

    def ps4_tile_launchable(self):
        """True / False / None (could not ask). Whether the icon would actually OPEN.

        INSTALLED AND LAUNCHABLE ARE DIFFERENT QUESTIONS, and only the console can answer the second.
        A PS4 refuses to start a title its download service has no task for - klog says
        "GameWillStart(...) -> ERROR: task not found" and the television says CE-32930-7 - so an app
        whose files are all present and correct can still do nothing when pressed. Measured on the
        hardware repeatedly. The payload reports `launchable` beside `registered` for exactly this."""
        if not self.is_ps4():
            return None
        try:
            d = self._shop("/api/tile/status", timeout=8) or {}
        except Exception:
            return None
        v = d.get("launchable")
        return bool(v) if v is not None else None

    def ps4_tile_stale(self):
        """True / False / None (cannot tell). Is the installed icon an OLDER BUILD than the one
        the console's own ELF carries?

        A THIRD QUESTION, and the one that went unasked. "Installed" and "will it open" were both
        true of this console while the icon it opened was a build from the day before, because
        nothing compared the bytes from this side and the console could not fix it alone: the
        download service refuses a package the console serves to itself once PlayStation Network
        is blocked, and the only other lane it has cannot create a launch ticket, so it correctly
        declines to replace a working icon with a worse one. It reports the mismatch instead.

        None whenever the console does not answer, is not a PS4, or is running the lite payload -
        which carries no package and so has nothing to compare. Never guessed: a wrong `True` here
        reinstalls the icon on a timer for ever."""
        if not self.is_ps4():
            return None
        try:
            d = self._shop("/api/tile/status", timeout=8) or {}
        except Exception:
            return None
        if not d.get("carries_package"):
            return None
        v = d.get("stale")
        return bool(v) if v is not None else None

    def install_ps4_tile(self, force=False):
        """Install the dashboard app on this PS4. Returns (ok, info)."""
        if not self.is_ps4():
            return False, {"error": "That console is not a PS4"}
        if not os.path.isfile(PS4_TILE_PKG):
            return False, {"error": "This copy of the app does not carry the PS4 dashboard app"}
        if not force and self.ps4_tile_installed():
            return True, {"already": True, "title_id": PS4_TILE_TID}
        url = "http://%s:%d/library/%s" % (companion_ip_for(self.ip),
                                           self.cfg["companion"]["port"], PS4_TILE_KEY)
        try:
            size = os.path.getsize(PS4_TILE_PKG)
        except OSError:
            size = 0
        return self.install_spawn(url, "PKG MUTANT SHOP", force=True,
                                  pkg={"content_id": PS4_TILE_CID, "size": size, "kind": "base"})

    def _reachable_fast(self, budget=0.35):
        """Is ANY transport to this console accepting connections right now?

        Deliberately cheap. A TCP connect to a machine that is off settles in milliseconds, while
        the FTP read this guards waits out its full eight seconds. That was tolerable with one
        console and is not with two: with a PS5 and a PS4 both switched off, rebuilding the library
        took sixteen seconds of pure timeout.

        It asks about every transport, not just the shop port, because a console whose ELF is not
        loaded yet is still readable over FTP - gating on the shop port alone would have broken the
        fallback that exists for exactly that case. Only when NOTHING accepts a connection is there
        anything left to skip.
        """
        if not self.ip:
            return False
        if self.up(budget=budget):
            return True
        ports, seen = [], set()
        for p in [self._ftp_port, self.c.get("ftp_port")] + list(self.FTP_FALLBACKS):
            try:
                p = int(p)
            except (TypeError, ValueError):
                continue
            if p not in seen:
                seen.add(p)
                ports.append(p)
        for p in ports:
            sock = socket.socket()
            sock.settimeout(budget)
            try:
                if sock.connect_ex((self.ip, p)) == 0:
                    return True
            except Exception:
                pass
            finally:
                try:
                    sock.close()
                except Exception:
                    pass
        return False

    def _ps4_console_apps(self):
        """The PS4's installed games, as our PS4 payload reports them. None if it did not answer.

        Same dict shape the PS5 path produces, so build_library, the storage tiles, the icon lane and
        already_installed all work without knowing which console they are looking at. The payload
        lists only real games (app.db category 'gd', never the 'gdi' system stubs) and tells us which
        of them have their own app.pkg on disk - the one honest proof a title is really installed, and
        the same rule the PS5 side uses.
        """
        try:
            j = self._shop("/api/library", timeout=25)
        except Exception:
            return None
        if not isinstance(j, dict) or not j.get("ok"):
            return None
        apps = []
        for g in (j.get("games") or []):
            tid = str(g.get("title_id") or "").strip()
            if not tid:
                continue
            cid = str(g.get("content_id") or "")
            try:
                size = int(g.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            apps.append({
                "title_id": tid, "content_id": cid, "name": g.get("name") or tid, "size": size,
                "region": pkg_meta.region_from_content_id(cid),
                # The PS4 payload reports the internal drive; a game on a USB disk is still
                # registered here, and build_storage buckets by the path it finds, not by this.
                "location": "0", "drive": self.LOC_LABEL["0"],
                "platform": "PS4",
                # install_status mirrors the PS5 field: 0 for a title with its data in place.
                "install_status": 0 if g.get("installed") else 1,
                "app_format_type": "app", "app_ver": str(g.get("installed_version") or ""),
                "icon_path": ("/user/appmeta/%s/icon0.png" % tid) if g.get("has_icon") else None,
                # No ShadowMount on the PS4, so nothing here is ever a mounted backup.
                "backup_path": None, "format": None, "source": "installed",
            })
        return apps

    def console_apps(self, force=False):
        """
        Read installed titles from the console's app.db (tbl_contentinfo, verified PS5 12.70 schema):
        title_id, name, size, install location. Cached 30s. Returns list of dicts, or None if unreachable.
        """
        if not self.ip:
            return None
        if not force and self._apps is not None:
            if time.time() - self._apps_ts < 30:
                return self._apps                       # warm cache → instant
            # stale: serve the stale list NOW, refresh once in the background (never block the request)
            if not self._apps_refreshing:
                self._apps_refreshing = True
                threading.Thread(target=self._bg_refresh_apps, daemon=True).start()
            return self._apps
        # A console that just failed is not going to answer three lines later. Without this memo
        # every call in a single request paid the full FTP timeout again, which is what made the
        # library take ~25s whenever the PS5 was off. `force` still ignores it.
        if not force and time.time() - getattr(self, "_apps_fail_ts", 0) < 10:
            return None
        # Nothing is listening: skip the transports rather than waiting for each to give up.
        if not self._reachable_fast():
            self._apps_fail_ts = time.time()
            return None
        # A PS4 STORES ITS INSTALLED GAMES IN A DIFFERENT SCHEMA, and our PS4 payload already reads
        # it. app.db sits at the same path on both consoles, but a PS4 has no tbl_contentinfo: it has
        # a per-user tbl_appbrowse_<userid> plus a key/value tbl_appinfo holding APP_VER. Pulling the
        # file here and re-implementing that in Python would be a second copy of logic the
        # console-side build already has right (and would have to track the user id), so the PS4 is
        # asked for the finished list instead. Everything below this line is the PS5 path, unchanged.
        if self.is_ps4():
            apps = self._ps4_console_apps()
            if apps is None:
                self._apps_fail_ts = time.time()
                return None
            self._apps, self._apps_ts = apps, time.time()
            self._apps_fail_ts = 0.0
            return apps
        db_path = self.cfg.get("console", {}).get("app_db_path", "/system_data/priv/mms/app.db")
        # Through _pull_db: our own on-console file API first, the third-party FTP only as a
        # fallback. Unique temp name per pull - the confirm loop, the library scan and the UI can
        # all be here at once, and one thread truncating a file another is mid-read on produces
        # "database disk image is malformed", which the caller reads as "not installed yet".
        tmp = self._pull_db_cached(db_path, force)
        if not tmp:
            self._apps_fail_ts = time.time()
            return None
        apps = []
        try:
            con = sqlite3.connect(tmp)
            cur = con.cursor()
            try:
                cur.execute("SELECT titleId,contentId,titleName,size,contentLocation,installStatus,"
                            "icon0Info,appFormatType,AppInfoJson FROM tbl_contentinfo")
                rows = cur.fetchall()
            except sqlite3.Error:
                rows = []
            for tid, cid, name, size, loc, st, icon, aft, aij in rows:
                if not tid:
                    continue
                # The version ACTUALLY installed (patches bump it). Without it we cannot tell
                # an applied patch from an available one, and the cheat engine cannot tell
                # whether a cheat file matches the build.
                #
                # PS4 and PS5 do NOT use the same field, which is why every PPSA title used to
                # report no version at all:
                #   PS4 (CUSA) -> "APP_VER"          e.g. 01.04
                #   PS5 (PPSA) -> "CONTENT_VERSION"  e.g. 01.011.000
                # Verified against this console: CONTENT_VERSION matches the cheat filenames
                # exactly (Doom 01.011.000, Hogwarts 01.000.010, Ratchet 01.005.003).
                app_ver = ""
                try:
                    info = json.loads(aij or "{}") or {}
                    app_ver = info.get("APP_VER") or info.get("CONTENT_VERSION") or ""
                except Exception:
                    m = (re.search(r'"APP_VER"\s*:\s*"([^"]+)"', aij or "")
                         or re.search(r'"CONTENT_VERSION"\s*:\s*"([^"]+)"', aij or ""))
                    app_ver = m.group(1) if m else ""
                loc = str(loc)
                plat = "PS5" if tid.startswith("PPSA") else ("PS4" if tid.startswith("CUSA") else "SYS")
                apps.append({"title_id": tid, "content_id": cid or "", "name": name or tid,
                             "size": int(size or 0),   # REAL installed size (app.db size == AppInfoJson #_size)
                             "region": pkg_meta.region_from_content_id(cid or ""),
                             "location": loc, "drive": self.LOC_LABEL.get(loc, "Storage " + loc),
                             "platform": plat, "install_status": st, "app_format_type": aft,
                             "app_ver": app_ver,
                             "icon_path": (icon or "").split("?")[0] or None})
            con.close()
        except sqlite3.Error:
            return None
        finally:
            try:
                os.remove(tmp)
            except OSError:
                pass
        # PS4 (CUSA) games the PS5 app.db drops on this jailbreak are still really installed - but
        # only the ones whose game payload is still on the drive. See console_ps4_installed().
        have = {a["title_id"] for a in apps}
        for tid, sz in self.console_ps4_installed(force).items():
            if tid not in have:
                # Say which drive it is really on. Hardcoding internal put every PS4 game on the
                # wrong storage tile the moment the user installed one to the M.2.
                _root = getattr(self, "_ps4loc", {}).get(tid, "")
                loc4 = "2" if _root.startswith("/mnt/") else "0"
                apps.append({"title_id": tid, "name": tid, "size": sz, "location": loc4,
                             "drive": self.LOC_LABEL.get(loc4, "Storage " + loc4),
                             "platform": "PS4", "install_status": 0,
                             "icon_path": "/user/appmeta/%s/icon0.png" % tid})
        # REAL size/format/path from ShadowMount backup files (app.db size is a bogus metadata value)
        # `force` is NOT passed on. Doing so forced the 12-folder backup scan (12 /api/fs/list
        # round trips on the console's single accept loop) on every forced app read - and
        # installed_titles(force=True) is polled every 5 s while a backup mounts and every heavy
        # tick while a game installs. The memo is dropped by invalidate_apps() instead, so the
        # one moment that needs a fresh scan (a delete, a mount) gets it without the rest paying.
        backups = self.console_backups() or {}
        for a in apps:
            b = backups.get(a["title_id"])
            if b:
                a["size"], a["backup_path"], a["format"], a["source"] = b["size"], b["path"], b["format"], "backup"
                # The DRIVE too. app.db records where the TITLE is registered ("Extended
                # Storage" for every backup here), not where the container is: 18 usb0 backups
                # read "Extended Storage" in the UI. The container's own path is the truth, and
                # build_storage already buckets by it. `drive_id` is additive.
                did = _drive_of_backup(b["path"])
                if did:
                    a["drive_id"] = did
                    if did.startswith("usb"):
                        a["drive"] = did.upper()
                    elif did == "internal":
                        a["drive"] = self.LOC_LABEL["0"]
                    else:
                        a["drive"] = self.LOC_LABEL["2"]
            else:
                a["backup_path"], a["format"], a["source"] = None, None, "installed"
        self._apps, self._apps_ts = apps, time.time()
        self._apps_fail_ts = 0.0            # it answered: stop holding the failure against it
        return apps

    def _bg_refresh_apps(self):
        """Background refresh for stale-while-revalidate. On FTP failure console_apps() returns None
        without touching self._apps, so the stale cache survives until the next successful refresh."""
        try:
            self.console_apps(force=True)
        finally:
            self._apps_refreshing = False

    def console_ps4_installed(self, force=False):
        """PS4 (CUSA) titles that are REALLY installed, as {title_id: bytes}. Cached 60s.

        The PS5's app.db omits PS4 games on this firmware, so it cannot answer this and something
        on the filesystem has to. Two candidates, and only one of them is true:

          /user/appmeta/<CUSA>/icon0.png   ARTWORK. Survives an uninstall and survives "reset
                                           database". Measured here: 55 of these, for 2 installed
                                           games. This is what the check used to use, and it is why
                                           53 titles the console does not have were shown as
                                           installed with Install offering their update.

          /user/app/<CUSA>/app.pkg         THE GAME. Measured here: 31 folders, 29 of them EMPTY,
                                           and exactly 2 with a non-empty app.pkg - CUSA02365 and
                                           CUSA58072, which is precisely what the console itself
                                           reports. The folder alone is not enough; the payload is.

        Its size is worth returning: app.db has no row for these titles, so this is the only real
        figure anyone has for them."""
        if not self.ip:
            return {}
        if not force and self._ps4meta is not None and time.time() - self._ps4meta_ts < 60:
            return self._ps4meta
        # EVERY root a PS4 game can live under - the same four that installed_app_pkg() and
        # ps5-app/onconsole/server.c already scan. Looking only at /user/app made every game on
        # extended storage read as not installed, which matters a great deal now: with the
        # console's Installation Location set to the M.2, that is where new games go.
        roots = ["/user/app", "/mnt/ext0/user/app", "/mnt/ext1/user/app", "/mnt/ext2/user/app"]
        found, locs = {}, {}
        # Through fs_list, so this works without a third-party FTP - and without parsing LIST
        # output, which is a formatted human string every server words differently.
        try:
            for base in roots:
                top = self.fs_list(base, timeout=20)
                if top is None:
                    if base == "/user/app":
                        raise OSError("cannot list %s" % base)
                    continue        # an absent extended root is just a drive that is not plugged in
                cands = [e["name"].upper() for e in top
                         if e.get("dir") and re.fullmatch(r"CUSA\d{5}", e.get("name") or "", re.I)]
                for tid in cands:
                    sub = self.fs_list("%s/%s" % (base, tid), timeout=15)
                    if sub is None:
                        # A FAILED READ IS NOT AN EMPTY FOLDER. Treating them alike dropped the
                        # title from console_apps(), and /api/installed writes that loss straight
                        # into installed.json - where nothing ever puts it back.
                        raise OSError("cannot list %s/%s" % (base, tid))
                    if not sub:
                        continue                   # a genuinely empty folder is a leftover
                    for e in sub:
                        if (not e.get("dir")) and (e.get("name") or "").lower() == "app.pkg":
                            sz = int(e.get("size") or 0)
                            if sz > 0:
                                found[tid] = sz
                                locs[tid] = base
                            break
        except Exception as exc:
            print("[ps4] install scan failed: %r" % (exc,))
            return self._ps4meta or {}
        self._ps4meta, self._ps4meta_ts = found, time.time()
        self._ps4loc = locs
        return found

    def console_backups(self, force=False):
        """Scan the ShadowMount backup folders for real game files (.ffpfsc etc). Map title_id -> real size."""
        if not self.ip:
            return None
        if not force and self._backups is not None and time.time() - self._backups_ts < 30:
            return self._backups
        sm = self.cfg.get("shadowmount", {})
        # Scan EVERY real backup location so .ffpfsc/.ffpfs/etc. are found regardless of config: the
        # configured path(s) + internal + ext0/ext1 + all USB slots. (Backups here live in /mnt/ext1/homebrew.)
        paths = list(sm.get("scan_paths") or [])
        for p in ([sm.get("scan_path"), "/data/homebrew", "/mnt/ext0/homebrew", "/mnt/ext1/homebrew"]
                  + ["/mnt/usb%d/homebrew" % i for i in range(8)]):
            if p and p not in paths:
                paths.append(p)
        result = {}
        any_dir = False
        for base in paths:
            rows = self.fs_list(base, timeout=15)
            if rows is None:
                continue                        # this folder does not exist on this console
            any_dir = True
            for e in rows:
                if e.get("dir"):
                    continue
                try:
                    size = int(e.get("size") or 0)
                except (TypeError, ValueError):
                    continue
                name = e.get("name") or ""
                # Same list the library scanner uses. These two disagreed, so a backup in a
                # format one accepted was invisible to the other.
                if not name.lower().endswith(MOUNT_EXTS):
                    continue
                m = re.search(r"(CUSA\d{5}|PPSA\d{5})", name, re.I)
                if m:
                    result[m.group(1).upper()] = {"size": size, "path": base + "/" + name,
                                                  "format": name.rsplit(".", 1)[-1].lower()}
        if not any_dir:
            return None                     # console unreachable, as opposed to "no backups"
        self._backups, self._backups_ts = result, time.time()
        return result

    def installed_titles(self, force=False):
        apps = self.console_apps(force=force)
        return None if apps is None else sorted(a["title_id"] for a in apps)

    ICON_MISS_TTL = 600.0

    def console_icon(self, title_id, fresh=False):
        """Fetch a game's real icon0.png from the console (cached in ICON_DIR). Returns local path or None.

        A MISS IS REMEMBERED for ICON_MISS_TTL seconds. build_library() sets has_icon for every
        installed title, so the UI asks for every cover; a title whose icon0.png is not there (or
        whose read failed) came back here on EVERY card render, each time paying console_apps()
        plus a 20 s fs_read against the PS5's single accept loop - while an install was using it.
        `fresh=True` ignores a remembered miss - for the just-installed case, where the art is
        expected to appear any second.
        """
        dest = os.path.join(ICON_DIR, title_id + ".png")
        if os.path.exists(dest) and os.path.getsize(dest) > 0:
            return dest
        misses = getattr(self, "_icon_miss", None)
        if misses is None:
            misses = self._icon_miss = {}
        now = time.time()
        if not fresh and now - misses.get(title_id, 0.0) < self.ICON_MISS_TTL:
            return None
        apps = self.console_apps()
        if not apps:
            return None                     # not a miss: the console did not answer at all
        path = next((a.get("icon_path") for a in apps if a["title_id"] == title_id), None)
        if not path:
            misses[title_id] = now
            return None
        try:
            os.makedirs(ICON_DIR, exist_ok=True)
            data = self.fs_read(path, timeout=20)
            if not data:
                misses[title_id] = now
                return None
            with open(dest, "wb") as f:
                f.write(data)
            if os.path.getsize(dest) > 0:
                misses.pop(title_id, None)
                return dest
            misses[title_id] = now
            return None
        except Exception:
            try:
                os.remove(dest)
            except OSError:
                pass
            misses[title_id] = now
            return None

    def console_cheats(self, title_id):
        """Read mods/patches for a title from the mod library over FTP, parsing REAL mod names. dict|None.
        Reads data files directly (not the Cheat Runner service) — path is configurable (cheats.root)."""
        if not self.ip or not title_id:
            return None
        root = self.cfg.get("cheats", {}).get("root", "/data/cheatrunner")
        tu = title_id.upper()
        def files_in(path):
            # fs_list, not NLST. NLST is the one FTP verb this console answers 502 to on several
            # of these paths, and its output is a bare name list with no way to tell a file from a
            # directory.
            rows = self.fs_list(path, timeout=12)
            if not rows:
                return []
            return sorted({e["name"] for e in rows
                           if not e.get("dir") and (e.get("name") or "").upper().startswith(tu)})

        cheats, patches = [], []
        for base in files_in(root + "/cheats/json"):     # JSON = richest: parse real mod names
            entry = {"file": base, "format": "json", "version": None, "process": None, "mods": [], "credits": []}
            try:
                d = json.loads(self.fs_text("%s/cheats/json/%s" % (root, base), timeout=12) or "{}")
                entry["version"], entry["process"] = d.get("version"), d.get("process")
                entry["credits"] = d.get("credits") or []
                for m in d.get("mods") or []:
                    entry["mods"].append({"name": m.get("name", ""), "type": m.get("type", ""),
                                          "patches": len(m.get("memory") or [])})
            except Exception:
                pass
            cheats.append(entry)
        for sub in ("shn", "mc4"):                        # other formats: listed (parsed later)
            for base in files_in("%s/cheats/%s" % (root, sub)):
                cheats.append({"file": base, "format": sub, "version": None, "process": None, "mods": [], "credits": []})
        for sub, tag in (("xml_orbis", "PS4"), ("xml_prospero", "PS5"), ("xml", "xml")):
            for base in files_in("%s/patches/%s" % (root, sub)):
                patches.append({"file": base, "platform": tag})
        seen = set()
        patches = [p for p in patches if not (p["file"] in seen or seen.add(p["file"]))]
        return {"cheats": cheats, "patches": patches}


class _ProgressReader(object):
    """A read-only file wrapper that reports bytes as they are handed to the socket.

    urllib streams a file-like body in 8 KB blocks when Content-Length is set, so wrapping the file
    is how the mount lane keeps its live percentage without buffering a multi-GB game in memory.
    `on_read` may raise (that is how cancel works) and the exception propagates out of the request.
    """

    def __init__(self, fh, on_read=None):
        self._fh = fh
        self._on_read = on_read
        self.total = 0

    def read(self, size=-1):
        chunk = self._fh.read(size)
        if chunk:
            self.total += len(chunk)
            if self._on_read:
                self._on_read(len(chunk), self.total)
        return chunk

    def __iter__(self):
        return self

    def __next__(self):
        chunk = self.read(65536)
        if not chunk:
            raise StopIteration
        return chunk

    next = __next__


class Fleet:
    def __init__(self, cfg):
        self.cfg = cfg
        self.reload()

    def reload(self):
        self.consoles = consoles_from_cfg(self.cfg)
        self.bridges = {c["id"]: Ps5Bridge(c, self.cfg) for c in self.consoles}

    def bridge(self, cid):
        """The bridge for this console id. None when the id is unknown and guessing is not safe.

        The fallback below is for the single-console case, where "whatever console there is" and
        "the one you asked for" are the same thing, and dropping it would break callers that pass a
        stale or empty id. With TWO consoles configured - which is what PS4 support makes ordinary -
        the same fallback would quietly hand a PS4's install to the PS5, so an unknown id becomes a
        refusal the caller can report instead."""
        if cid in self.bridges:
            return self.bridges[cid]
        if len(self.bridges) == 1:
            return next(iter(self.bridges.values()))
        return None

    def ids(self):
        return [c["id"] for c in self.consoles]

    def status(self):
        # probe all consoles in parallel so /api/consoles isn't N × ping-timeout
        results = {}

        def probe(c):
            results[c["id"]] = dict(c, online=self.bridges[c["id"]].engine_available())

        threads = [threading.Thread(target=probe, args=(c,)) for c in self.consoles]
        for t in threads:
            t.start()
        # THE JOIN HAS TO OUTLAST THE PROBE IT IS WAITING FOR. This waited 2 s while
        # engine_available() is up() (0.6 s budget) followed by _shop("/api/health", timeout=4), so
        # a console that was merely BUSY - mid-install, or rebuilding its library - overran and was
        # recorded as offline. join() returns the moment a thread finishes, so a healthy fleet
        # costs exactly what it did; this ceiling is only ever paid by a console that is genuinely
        # slow, and such a console was being given a wrong answer at 2 s instead of a right one at
        # 4.6.
        for t in threads:
            t.join(timeout=4.6)
        # AND "DID NOT FINISH" IS NOT "SAID NO". A probe still running now reports online: None -
        # unknown - which is the three-state rule /api/health's console_list already uses. None is
        # falsy, so every `if c.get("online")` test and the settings LED read exactly as they did;
        # what changes is that nothing declares a console DOWN on no evidence.
        return [results.get(c["id"], dict(c, online=None)) for c in self.consoles]



# --------------------------------------------------------------------------- #
# PSN blocker - a DNS server that answers everything except PlayStation        #
# --------------------------------------------------------------------------- #
#
# WHY THIS IS IN A GAME INSTALLER AT ALL.
#
# A jailbroken PS4 that can reach PlayStation Network does two things that make it unusable as a
# homebrew console, and both were measured on the owner's machine rather than read about:
#
#   * IT REFUSES TO INSTALL. Before downloading anything the console asks PSN whether the title has
#     a newer version. When it finds one it builds a task with a second piece in it - the update -
#     fetches the package it was given, cannot fetch the update, and abandons the whole install.
#     Bluey's Quest for the Gold Pen: a 737,869,824-byte package, the console demanded 1,348,665,344,
#     and gave up at 89%. Castle Crashers Remastered: the same at 2%. Riptide GP2 installed first
#     time, because the Store has no newer version of it. That is the entire difference.
#   * IT DELETES WHAT IS ALREADY INSTALLED. After a restart, every fake-signed RETAIL title was gone
#     from /user/app, /user/appmeta and app.db - three of them - while the owner's own Store game and
#     our homebrew-prefixed app survived untouched.
#
# THE OBVIOUS FIX MAKES THINGS WORSE. Setting the console's DNS to 127.0.0.1 stops it reaching PSN
# and also stops it reaching everything else, including our own page on the LAN: the console's
# connection check fails and the browser then answers WV-33920-7 for every address, IP or not.
# A public "update blocker" DNS has the same shape of problem - it is someone else's server, it may
# be down, and it decides for you what is blocked.
#
# So the companion answers DNS itself. Sony's names get NXDOMAIN; everything else is forwarded to a
# real resolver and relayed back untouched. The console keeps working - browser, LAN, our page - and
# simply cannot find PlayStation Network. It also cannot find a system update, which is what the
# owner wanted anyway.
#
# It listens on the LAN address rather than 0.0.0.0 on purpose: Windows already has something on
# 0.0.0.0:53 (svchost) on this machine, and binding the specific address both coexists with it and
# keeps this off the loopback interface. A bind that fails is logged and the rest of the app carries
# on - a game installer must not refuse to start because a side feature could not open a port.
PSN_BLOCK_SUFFIXES = (
    "playstation.net",
    "playstation.com",
    "sonyentertainmentnetwork.com",
    "scea.com",
    "scee.com",
    "np.community.playstation.net",
)


class DnsBlocker(threading.Thread):
    """Answers DNS for the consoles: NXDOMAIN for Sony, forwarded for everything else."""

    daemon = True

    def __init__(self, cfg):
        threading.Thread.__init__(self, name="dns-blocker")
        self.cfg = cfg
        b = cfg.get("psn_block") or {}
        self.enabled = bool(b.get("enabled", True))
        self.listen_ip = str(b.get("listen_ip") or "") or None
        self.upstream = str(b.get("upstream") or "1.1.1.1")
        self.extra = tuple(x.lower().strip(".") for x in (b.get("extra_blocked") or []) if x)
        self.sock = None
        self.bound = None
        self.blocked_count = 0
        self.passed_count = 0
        self.last_blocked = ""
        self.error = ""

    # ---- wire format, only as much of it as this needs
    @staticmethod
    def _qname(data):
        """The queried name, and the offset just past it. ('' , 0) when it cannot be read."""
        try:
            i, parts = 12, []
            while True:
                n = data[i]
                if n == 0:
                    i += 1
                    break
                if n & 0xC0:                      # a pointer has no business in a question
                    return "", 0
                parts.append(data[i + 1:i + 1 + n].decode("ascii", "replace"))
                i += 1 + n
                if i > len(data) or len(parts) > 40:
                    return "", 0
            return ".".join(parts).lower(), i
        except Exception:
            return "", 0

    def _is_blocked(self, name):
        for suf in PSN_BLOCK_SUFFIXES + self.extra:
            if name == suf or name.endswith("." + suf):
                return True
        return False

    @staticmethod
    def _nxdomain(data, qend):
        """The same question back with RCODE 3. Echoing the question is what makes a resolver
        accept the answer rather than retry it forever."""
        ident = data[0:2]
        rd = data[2] & 0x01                        # keep the caller's recursion-desired bit
        flags = bytes([0x81 | 0x00, 0x80 | 0x03]) if rd else bytes([0x80, 0x03])
        head = ident + flags + b"\x00\x01" + b"\x00\x00" + b"\x00\x00" + b"\x00\x00"
        return head + data[12:qend + 4]

    def _forward(self, data):
        up = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        up.settimeout(3.0)
        try:
            up.sendto(data, (self.upstream, 53))
            reply, _ = up.recvfrom(4096)
            return reply
        finally:
            up.close()

    def run(self):
        if not self.enabled:
            return
        ip = self.listen_ip or _lan_ip()
        try:
            srv = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            srv.bind((ip, 53))
            srv.settimeout(1.0)
        except Exception as e:
            self.error = str(e)
            print(" PSN blocker: could not listen on %s:53 (%s) - point the console's DNS "
                  "somewhere else, or turn this off" % (ip, e))
            return
        self.sock, self.bound = srv, ip
        print(" PSN blocker: answering DNS on %s:53 - PlayStation Network is unreachable for any "
              "console pointed here, everything else is forwarded to %s" % (ip, self.upstream))
        while True:
            try:
                data, peer = srv.recvfrom(2048)
            except socket.timeout:
                continue
            except Exception:
                time.sleep(0.2)
                continue
            if len(data) < 13:
                continue
            name, qend = self._qname(data)
            try:
                if name and self._is_blocked(name):
                    self.blocked_count += 1
                    self.last_blocked = name
                    srv.sendto(self._nxdomain(data, qend), peer)
                else:
                    self.passed_count += 1
                    srv.sendto(self._forward(data), peer)
            except Exception:
                # A forward that fails is not worth an answer we made up.
                pass

    def status(self):
        return {"enabled": self.enabled, "listening_on": self.bound, "upstream": self.upstream,
                "blocked": self.blocked_count, "forwarded": self.passed_count,
                "last_blocked": self.last_blocked, "error": self.error,
                "suffixes": list(PSN_BLOCK_SUFFIXES) + list(self.extra)}


def _lan_ip():
    """This machine's address on the LAN - the one a console can reach."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))
        return s.getsockname()[0]
    except Exception:
        return "0.0.0.0"
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# install queue - parallel, per-console serialized, unified real progress      #
# --------------------------------------------------------------------------- #
class Queue:
    def __init__(self, fleet, transfers, cfg):
        self.fleet = fleet
        self.transfers = transfers
        self.cfg = cfg
        self.max_parallel = max(1, cfg["queue"].get("max_parallel", 2))
        self.allow_ppc = bool(cfg["queue"].get("allow_parallel_per_console", False))
        self.tasks = {}
        self.order = []
        self.active = set()          # consoles with an install in flight
        self.lock = threading.Lock()
        self._seq = 0
        for _ in range(self.max_parallel):
            threading.Thread(target=self._worker, daemon=True).start()

    def add(self, task):
        with self.lock:
            self._seq += 1
            tid = "job-%d" % self._seq
            held = bool(task.get("hold"))                      # [B8] add-to-queue vs install-now
            task.update({"id": tid, "state": "held" if held else "queued", "pct": 0,
                         "msg": "Held — press Start" if held else "Queued",
                         "cancel": False, "simulated": False, "ts": now_ms()})
            self.tasks[tid] = task
            self.order.append(tid)
        return tid

    def start_held(self):
        """Release all held tasks into the run queue, SMALLEST game first (smart order); the worker then
        installs them one at a time per console. [B8]"""
        with self.lock:
            held = [self.tasks[i] for i in self.order if self.tasks[i]["state"] == "held"]
            held.sort(key=lambda t: t.get("total") or 0)
            for t in held:
                t["state"], t["msg"] = "queued", "Queued"
            ids = set(t["id"] for t in held)
            self.order = [i for i in self.order if i not in ids] + [t["id"] for t in held]
            return len(held)

    def snapshot(self):
        """The queue as the outside sees it: a COPY of each task, without the worker's private
        '_' keys. The live dicts used to be handed straight to json.dumps while a worker was
        writing them, and every internal key (bgft row tuples, the pre-handoff snapshot) shipped
        to every client and peer with each 1.2 s poll. Readers inside this file that need the
        live object (cancel, retry, _pause_pending) go through self.tasks under the lock."""
        with self.lock:
            return [{k: v for k, v in self.tasks[i].items() if not str(k).startswith("_")}
                    for i in self.order]

    # Genuinely finished states. A task in one of these has nothing left to stop.
    TERMINAL = ("playable", "error", "canceled")

    def cancel(self, tid):
        """Stop a task. Returns True when something was actually stopped.

        Two things were wrong here. "submitted" was treated as terminal, so pressing x on the one
        state that most needs it - handed to the console, no progress, spins forever - returned
        False and the UI said nothing at all. And a task that had not been claimed yet only got a
        flag, so its row went on reading "Queued" until a worker eventually picked it up; now it is
        marked cancelled straight away. A task that IS running still gets the flag, because its
        lane has to unwind its own transfer.
        """
        with self.lock:
            t = self.tasks.get(tid)
            if not t or t["state"] in self.TERMINAL:
                return False
            t["cancel"] = True
            was = t["state"]
            if was in ("queued", "held", "submitted"):
                t["state"] = "canceled"
                t["msg"] = ("Canceled - the console may still finish what it already accepted"
                            if was == "submitted" else "Canceled")
                t["ts"] = now_ms()
            return True

    def retry(self, tid):
        """Re-queue a failed / canceled / stalled task — e.g. after the user reloads a wedged DPI payload,
        press retry on Dark Souls and it hands off again (small-first ordering keeps it after any running). """
        with self.lock:
            t = self.tasks.get(tid)
            if t and t["state"] in ("error", "canceled", "submitted"):
                t.update(state="queued", msg="Queued (retry)", pct=0, cancel=False, fail_reason=None)
                if tid in self.order:
                    self.order.remove(tid)
                    self.order.append(tid)
                return True
        return False

    def _pause_pending(self, console, msg):
        """After a DPI wedge, HOLD every still-pending install for this console so they don't cascade-fail.
        The daemon can't self-recover — the user reloads the payload then presses ▶ Start to release them
        all (small-first). [wedge-pause]

        Was decorated @staticmethod while declaring `self`, so every one of its four callers
        (self._pause_pending(console, msg)) raised TypeError inside _run: the worker's catch-all
        turned each protective hold - no verdict, unknown host, wedge, console offline - into
        "This one stopped unexpectedly — press retry" and nothing behind it was ever paused."""
        with self.lock:
            for i in self.order:
                t = self.tasks.get(i)
                if t and t.get("lane") == "install" and t.get("console") == console and t["state"] == "queued":
                    t.update(state="held", msg=msg, pct=0)

    def clear_done(self):
        """Drop only what is genuinely FINISHED - which is what the button says.

        It used to keep a whitelist (RUNNING + "queued") and delete everything else, so it also
        deleted every HELD job. Build a queue with "+ Queue" (all held), press Clear finished to
        tidy one failed row, and the whole queue is gone server-side. Worse after a wedge, where
        _pause_pending deliberately holds every pending install: the one button offered to tidy up
        is the one that throws the batch away. "reloading" and "submitted" were in the same
        blast radius, and a reloading job is actively being worked on.
        """
        with self.lock:
            keep = [i for i in self.order if self.tasks[i]["state"] not in self.TERMINAL]
            self.tasks = {i: self.tasks[i] for i in keep}
            self.order = keep

    def dismiss(self, tid):
        """Remove a single FINISHED task from the list. Returns False while it is still running.

        clear_done() drops every finished task at once, which is the wrong granularity for "I want
        this one row gone". Refusing while a task is live is deliberate: the worker still holds a
        reference and goes on calling _set() on it, and a task that vanishes underneath its own
        worker is how a queue starts reporting states for jobs nobody can see.
        """
        with self.lock:
            t = self.tasks.get(tid)
            if not t:
                return True                       # already gone; the caller got what it wanted
            if t["state"] not in self.TERMINAL:
                return False
            self.tasks.pop(tid, None)
            self.order = [i for i in self.order if i != tid]
            return True

    def _finished_installing(self, t):
        """Anything that just landed on the console invalidates what we think is installed."""
        try:
            b = self.fleet.bridge(t.get("console"))
            if b:
                b.invalidate_apps()
            lib = getattr(self, "library", None)
            if lib is not None:
                lib.gen += 1          # nudges the UI to reload without waiting for a poll
                # STAMPED, so the console tracker does not toast the same install again forty-five
                # seconds later when apps_sig catches up. See note_content_change.
                lib._told_ms = time.time()
        except Exception:
            pass
        # A backup carries no artwork of its own — the console only creates
        # /user/appmeta/<id>/icon0.png once the title is actually there. Now that it is, grab the
        # icon straight away and rescan, so the card gets its real art immediately instead of
        # keeping its initials until something else happens to trigger a fetch. Once cached here it
        # is advertised to every other device as icon_url, exactly as for any other title.
        def _art():
            try:
                tid = t.get("title_id")
                b2 = self.fleet.bridge(t.get("console"))
                if not (tid and b2):
                    return
                for _ in range(10):                 # ShadowMount needs a moment to register it
                    if b2.console_icon(tid, fresh=True):   # a miss a moment ago is expected here
                        lib2 = getattr(self, "library", None)
                        if lib2 is not None:
                            lib2.scan()             # has_icon is read from disk at scan time
                        return
                    time.sleep(6)
            except Exception:
                pass
        threading.Thread(target=_art, daemon=True).start()

    def _set(self, t, **kw):
        with self.lock:
            t.update(kw)

    def _claim(self):
        with self.lock:
            if sum(1 for t in self.tasks.values() if t["state"] in RUNNING) >= self.max_parallel:
                return None
            for i in self.order:
                t = self.tasks[i]
                if t["state"] != "queued":
                    continue
                cid = t["console"]
                if (not self.allow_ppc) and cid in self.active:
                    continue
                t["state"] = "claimed"
                self.active.add(cid)
                return t
        return None

    def _release(self, t):
        with self.lock:
            self.active.discard(t["console"])

    def _worker(self):
        while True:
            job = self._claim()
            if not job:
                time.sleep(0.35)
                continue
            try:
                self._run(job)
            except Exception as e:
                # The repr is genuinely the useful thing here, so it goes to pms.log; the queue row
                # gets words. Showing a Python exception to someone installing a game tells them
                # nothing they can act on and looks like the app broke rather than the transfer.
                print("[queue] worker crashed on %r: %r" % (job.get("name"), e))
                self._set(job, state="error", detail=repr(e)[:300],
                          msg="This one stopped unexpectedly — press retry to try it again")
            finally:
                self._release(job)

    def _progress(self, t):
        # The engine=="pms" branch that stood here read the console's own /api/engine/job. No
        # task has ever been created with an `engine` key (grep: only /api/health and the mods
        # routes use that word), so it never ran; the spawn lane's progress is the LAN byte
        # counter below, and the mount lane reads engine_job() itself in _run_mount.
        key = t.get("key")
        if t.get("local_progress") and key is not None:
            # Our own console's bucket first; the shared aggregate only as a fallback for a job
            # whose console has no resolvable address (and for single-console setups before the
            # first byte, where the per-IP bucket does not exist yet).
            b = self.fleet.bridge(t.get("console"))
            counter = None
            if b is not None and b.ip:
                counter = self.transfers.get("%s|%s" % (key, b.ip))
            if counter is None:
                counter = self.transfers.get(key)
            return self._with_console_verdict(t, b, counter)
        if t.get("progress_url"):
            # ASK THE PEER FOR THIS CONSOLE'S BYTES, not for the file's. The peer already keeps a
            # counter per requesting address as well as the shared by-key one, but until this was
            # sent it could only answer the shared one - so two consoles pulling the same package
            # from the same peer both watched whichever transfer was further along. The query
            # string is stripped before that route matches, so a peer on an older build simply
            # answers the aggregate it always answered, and so does a new one that has not yet sent
            # this console a byte.
            _url = t["progress_url"]
            _b = self.fleet.bridge(t.get("console"))
            if _b is not None and _b.ip:
                _url += ("&" if "?" in _url else "?") + "console=" + quote(_b.ip)
            return http_get_json(_url)
        return None

    def _with_console_verdict(self, t, bridge, counter):
        """Fold a PS4's own install verdict into the byte counter the transfer loop watches.

        The loop already knows what to do with `state == "error"` - it stops and shows the message -
        but nothing ever put one there for this lane, so a failure could only ever surface as the
        generic "nothing arrived for 90 seconds". On the PS4 there IS a real verdict to read: the
        console names the one case it refuses (a title the Store has a newer version of), and that
        sentence is far more use than a stall.

        PS5 ONLY GETS THE COUNTER, exactly as before. Its payload answers on a single accept loop
        that is already busy installing, and this runs every half second - adding a round trip to it
        would be paid in the frames of whatever the user is looking at. Nothing about the PS5 path
        changes.

        Memoised for three seconds, so a half-second loop costs at most one question per three.
        """
        if bridge is None or not bridge.is_ps4():
            return counter
        memo = getattr(self, "_job_memo", None)
        if memo is None:
            memo = self._job_memo = {}
        now = time.time()
        seen = memo.get(t["id"])
        if not seen or now - seen[0] > 3.0:
            job = bridge.engine_job() or {}
            memo[t["id"]] = (now, job)
        else:
            job = seen[1]
        if not isinstance(job, dict) or not job.get("state"):
            return counter
        out = dict(counter or {})
        out["state"] = job.get("state")
        if job.get("msg"):
            out["msg"] = job["msg"]
        return out

    def _run(self, t):
        # Cancel is a flag, and until now nothing looked at it until well after the handoff. A job
        # cancelled while it sat in the queue was still claimed, still preflighted and still POSTed
        # to the install host - the console really installed it - and the row then said "Canceled".
        # One check, before any lane can do anything.
        if t.get("cancel"):
            self._set(t, state="canceled", pct=0, msg="Canceled before it started")
            return
        if t.get("lane") == "mount":
            return self._run_mount(t)
        if t.get("lane") == "console-local":
            return self._run_console_local(t)
        if t.get("lane") == "pc-copy":
            return self._run_pc_copy(t)
        if t.get("lane") == "peer-mount":
            return self._run_peer_mount(t)
        if t.get("lane") == "peer-delegate":
            return self._run_peer_delegate(t)
        bridge = self.fleet.bridge(t["console"])
        if bridge is None:
            self._set(t, state="error", msg="No console configured")
            return
        # Our engine lives in OUR on-console ELF, so "is the console reachable" is the only
        # question that matters. We deliberately do NOT probe :12800 or try to revive a
        # third-party daemon here — doing that is what silently launched Elf Arsenal (and
        # garlic / nanodns / ftpsrv / dpiv2 with it) the moment an install was queued.
        # ONE LANE. Our engine needs exactly one thing to be true - PKG MUTANT SHOP answering on
        # the console - and there is no daemon to probe, revive, reconfigure or heal. Everything
        # that used to happen here existed to keep somebody else's payload alive.
        online = bridge.engine_available()
        key, total = t.get("key"), t.get("total") or 0

        if online and t.get("url"):
            # [B7] integrity: verify the local PKG is structurally complete before the handoff. Default
            # "warn" — surfaces the verdict but NEVER blocks the proven path; set integrity.on_local_corrupt
            # ="block" to enforce (blocks only a *confident* truncation / bad-magic / missing-content-id).
            integ = self.cfg.get("integrity", {})
            lib = getattr(self, "library", None)
            local = lib.file_registry.get(key) if (lib and key) else None
            if integ.get("enabled", True) and integ.get("verify_before_install", True) \
                    and local and local.lower().endswith(".pkg") and os.path.isfile(local):
                self._set(t, state="verifying", msg="Verifying integrity…")
                verdict = pkg_meta.pkg_completeness(local)
                t["integrity"] = verdict
                if verdict.get("confident") and not verdict.get("complete"):
                    if integ.get("on_local_corrupt", "warn") == "block":
                        self._set(t, state="error", fail_reason="local_corrupt",
                                  msg="Integrity: %s — fix the file, then retry" % verdict.get("reason"))
                        return
                    self._set(t, msg="⚠ integrity: %s (installing anyway)" % verdict.get("reason"))
            # CANCEL, checked at every phase boundary and not only at the top. Between the check
            # above and the hand-off sit the integrity read, an app.db pull, up to three bgft
            # pulls and a 30 s lane wait - a cancel pressed anywhere in there used to be read
            # only AFTER the console had the package, and the row said "Canceled" over a game
            # that installed.
            if t.get("cancel"):
                self._set(t, state="canceled", pct=0, msg="Canceled before it started")
                return
            # [B9] SELF-HEALING HANDOFF. The DPI daemon wedges after a heavy install and never
            # recovers on its own, which is what used to strand every following install. So before
            # handing off we check the install lane itself (~15ms when healthy) and, if it is
            # wedged, reload the daemon automatically — kill the process that owns the port, then
            # relaunch the payload — with no user interaction. A healthy console is never touched.
            # NOTHING TO BRING UP. The three blocks that stood here - a proactive daemon reload,
            # a "stop the install host crashing its own job queue" preflight, and a wedge-heal -
            # all existed to nurse a third-party payload through an install. Our installer is
            # spawned per install and has already exited by the time anyone could reload it.

            self._set(t, state="submitting", msg="Handing off to %s" % bridge.name)
            if t.get("local_progress") and key is not None:
                # Reset OUR console's bucket only. Resetting the shared one is what made a second
                # console's hand-off zero the first console's in-flight counter.
                _b = self.fleet.bridge(t.get("console"))
                _bk = ("%s|%s" % (key, _b.ip)) if (_b is not None and _b.ip) else key
                self.transfers[_bk] = {"max": 0, "total": total or 1}
            # Remember whether the console already had this title. A registered title makes the
            # installer skip the download entirely and keep the old files, so afterwards its mere
            # presence in app.db must not be read as "this install worked".
            try:
                t["_pre_registered"] = bool(t.get("title_id")) and \
                    t.get("title_id") in (bridge.installed_titles(force=True) or [])
            except Exception:
                t["_pre_registered"] = False
            # An ADD-ON cannot be proven by app.pkg: the base game's app.pkg is already on disk and
            # is always far larger than the add-on's package, so a size check passes instantly and
            # means nothing. What proves it is a NEW bgft row of its own. Snapshot the newest row
            # before we hand over, so an old row from the base install cannot be mistaken for ours.
            # Distinguish "this title has no bgft row" from "bgft.db could not be read". Both
            # were None, and an unreadable snapshot turns the add-on freshness test (row != before)
            # into a test that any pre-existing completed row passes instantly. Retry first - the
            # usual cause is a momentary FTP hiccup - and record whether we ever got an answer.
            t["_bgft_before"], t["_bgft_before_ok"] = None, False
            if t.get("title_id"):
                for _try in range(3):
                    try:
                        t["_bgft_before"] = bridge.install_job_row(t.get("title_id"))
                        t["_bgft_before_ok"] = True
                        break
                    except Exception:
                        time.sleep(1.0)

                # IS THE CONSOLE ALREADY BUSY WITH THIS TITLE? This is the question the duplicate
                # guard should always have asked. Handing a package over while its own BGFT job is
                # still running is what produced several rows for one content id, all stuck at zero
                # bytes - and it is the shape of the submission that preceded the 2026-08-25 crash.
                #
                # Reading it here costs nothing: the row above was fetched anyway. Statuses:
                # 1000 parked/queued, 1009 downloading or installing; 1036/1026 are finished and
                # 1021/1022 failed or aborted, none of which is a reason to refuse.
                _row = t["_bgft_before"]
                if _row and not t.get("force") and int(_row[0] or 0) in (1000, 1009):
                    _pct = (100.0 * (_row[1] or 0) / _row[2]) if (_row[2] or 0) else 0.0
                    if bgft_row_is_dead(_row):
                        # QUEUED AND NEVER STARTED - see bgft_row_is_dead(). Not something to wait
                        # for, and the old wording said the opposite in every clause.
                        # NAMED FROM THE BRIDGE, because this lane runs for a PS4 too and this
                        # sentence told its owner to go and look at a PS5. is_ps4() is the one
                        # platform test this file keeps - a console that does not say what it is,
                        # is a PS5 - so for a PS5 this formats to the sentence it always was.
                        _w = "PS4" if bridge.is_ps4() else "PS5"
                        _msg = ("The %s has a stalled download for this game that never started, "
                                "and it is blocking new ones. Cancel it on the %s (Downloads, "
                                "select it, Cancel), then install this again." % (_w, _w))
                        print("[install] %s has a DEAD bgft row (status %s, 0 bytes) - not a live job"
                              % (t.get("title_id"), _row[0]))
                        self._set(t, state="error", pct=0, fail_reason="console_stalled_download",
                                  msg=_msg)
                        return
                    _msg = ("The console is already installing this - %.0f%% done. It will finish "
                            "on its own; there is nothing to send again." % _pct)
                    print("[install] %s already has a live bgft job (status %s, %.0f%%) - holding"
                          % (t.get("title_id"), _row[0], _pct))
                    self._set(t, state="held", pct=int(_pct), fail_reason="console_busy", msg=_msg)
                    return
                if not t["_bgft_before_ok"]:
                    print("[install] could not read bgft.db before handoff - this install will "
                          "not be auto-confirmed from a bgft row")
            else:
                t["_bgft_before_ok"] = True       # nothing to compare against, by design
            # Rebuild our own LAN URL now, not when the job was queued. A job that waited in
            # the queue overnight was still holding the address this PC had at the time.
            if t.get("relan") and t.get("key"):
                fresh = "http://%s:%d/library/%s" % (companion_ip_for(bridge.ip),
                                                     self.cfg["companion"]["port"], t["key"])
                if fresh != t.get("url"):
                    print("[install] our address changed since this was queued: %s -> %s"
                          % (t.get("url"), fresh))
                    t["url"] = fresh
            if t.get("cancel"):
                self._set(t, state="canceled", pct=0, msg="Canceled before it started")
                return
            t["_submitted_at"] = time.time()
            # One line per hand-off, and one per verdict below. Only the failure branches used
            # to print, so a successful install left no trace in pms.log of which package went
            # to which console over which address - the first thing anyone needs afterwards.
            print("[install] handoff %s: %s (%s) -> %s via %s"
                  % (t.get("id"), t.get("name"), t.get("title_id") or "no-tid", bridge.name,
                     t.get("url")))
            ok, res = bridge.install(t["url"], t.get("name") or "", force=bool(t.get("force")),
                                     cancelled=lambda: bool(t.get("cancel")),
                                     pkg={"content_id": t.get("content_id") or "",
                                          "size": t.get("pkg_size") or t.get("total") or 0,
                                          "kind": t.get("kind") or "base"})
            print("[install] verdict %s: ok=%s %s" % (t.get("id"), ok,
                  {k: v for k, v in (res or {}).items() if k != "detail"} if isinstance(res, dict)
                  else res))
            if not ok and isinstance(res, dict) and res.get("canceled"):
                # Stopped inside the lane wait, before the console was asked for anything.
                self._set(t, state="canceled", pct=0, msg="Canceled before it started")
                return
            if not ok:
                # Report the DPI host's *actual* failure mode instead of guessing. `res` carries either a
                # transport error (unreachable / timed out) or the daemon's own rejection payload. A bare
                # ping only proves the port is bound — a wedged daemon still accepts TCP but never answers,
                # so we look at *how* install() failed, not just whether the port is open.
                detail = ""
                if isinstance(res, dict):
                    detail = str(res.get("error") or res.get("res") or res.get("hint") or res).strip()
                elif res:
                    detail = str(res).strip()
                low = detail.lower()
                kindf = res.get("kind") if isinstance(res, dict) else None

                # THE CONSOLE REFUSED BECAUSE IT IS BUSY. It is reachable and healthy; something
                # it accepted earlier is still in flight. Hold this job and let the queue try again
                # later - never reload the ELF, because reloading resets the very latch that is
                # protecting the console (that is literally what happened on 2026-08-25).
                if isinstance(res, dict) and res.get("busy"):
                    self._set(t, state="held", pct=0, fail_reason="console_busy", msg=detail)
                    return

                # NO VERDICT. The installer was started and never reported back, so we do not know
                # whether the console is mid-install. Submitting again is how one package becomes
                # three BGFT jobs for one content id, all stuck at zero bytes, blocking each other.
                if isinstance(res, dict) and res.get("no_verdict"):
                    self._set(t, state="held", pct=0, fail_reason="dpi_no_verdict", detail=detail,
                              msg=detail)
                    self._pause_pending(t.get("console"), detail)
                    return

                # No host answered. Nothing was sent anywhere, so this is not a wedge and there is
                # nothing to reload — hold the queue and say what to do. Previously UNKNOWN fell
                # through to Arsenal's protocol and produced a connection reset, which the
                # substring classifier below then read as "wedged" and re-POSTed.
                if kindf == "host_unknown":
                    self._set(t, state="held", pct=0, fail_reason="dpi_host_unknown", msg="⏸ " + detail)
                    self._pause_pending(t.get("console"), detail)
                    return

                # THE CONSOLE VALIDATES BEFORE QUEUEING, and says so: queued=False means it
                # created no bgft job at all. That is the one case where a clean re-POST is provably
                # safe — the duplicate-job hazard only exists for a host that queues first and
                # rejects afterwards. One retry, then report the console's real error.
                #
                # Not when the refusal carries an SCE code (`rc`). That is the spawned installer's
                # own deterministic verdict - out of space, not a package, a sick app database -
                # and re-spawning it only produced a second identical refusal five seconds later
                # (pms.log: Red Dead Redemption 2, 0x80B21104, "retrying once", same answer).
                if isinstance(res, dict) and res.get("queued") is False and not t.get("_requeued") \
                        and res.get("rc") is None:
                    t["_requeued"] = True
                    print("[install] host rejected before queueing (%s) — retrying once" % detail)
                    self._set(t, state="submitting", pct=0,
                              msg="%s refused it (%s) — trying once more" % (bridge.name, detail[:60]))
                    time.sleep(2)
                    return self._run(t)

                timed_out = any(s in low for s in ("timed out", "timeout", "aborted", "reset", "10053",
                                                   "10054", "winerror", "unreachable", "no response", "refused"))
                seg = os.path.basename(t.get("url", ""))
                risky = any(ch in seg for ch in ("%20", "%5B", "%5D", "%28", "%29", " ", "[", "]"))
                # "Is it there" means one thing now: is OUR shop answering on the console.
                if not bridge.engine_available():
                    print("[install] fail (down) name=%r res=%r" % (t.get("name"), res))
                    self._set(t, state="error", fail_reason="dpi_down", detail=detail or None,
                              msg="The console stopped answering. Load PKG MUTANT SHOP on the %s%s, "
                                  "then press Start queue."
                                  % (bridge.kind_label(), bridge.reload_suffix()))
                    return
                # The console is there and said no. `detail` already carries its own sentence
                # (the on-console lane decodes the SCE code into words before it ever leaves the
                # PS5), so pass it through rather than paraphrasing it into something vaguer.
                if detail:
                    print("[install] fail (refused) name=%r res=%r" % (t.get("name"), res))
                    self._set(t, state="error", fail_reason="pkg_rejected", detail=detail,
                              msg=detail if detail[:1].isupper() else
                                  ("The console refused this package — %s" % detail))
                    return
                if timed_out:
                    # The port accepts TCP (even answers HEAD) but the install route HANGS: the daemon's
                    # install machinery is wedged — typical after a heavy install on 12.70, and it does NOT
                    # recover on its own. Reload it automatically and re-submit this same task once; only if
                    # that fails do we hold the queue and ask for a manual reload.
                    # NOTHING TO RELOAD. This block used to kill a wedged daemon and relaunch its
                    # payload, because a third-party install host on 12.70 stops accepting work
                    # after a heavy install and never recovers. Our installer is a fresh process
                    # per install that has already exited, so "wedged" is not a state it can be in
                    # - and the reload it used to perform is exactly what kept resurrecting etaHEN.
                    hold = ("\u23f8 The console took this package but never finished the hand-off. "
                            "Check the %s, then press Start queue to try it again."
                            % bridge.kind_label())
                    print("[install] no verdict — pausing queue. name=%r" % t.get("name"))
                    self._set(t, state="held", msg=hold, pct=0, fail_reason="dpi_wedged")
                    self._pause_pending(t.get("console"), hold)
                    return
                # A 4xx right after a previous install is the daemon wedging, NOT a bad package:
                # its install machinery stops accepting work and answers 400 to everything. Reload
                # it and re-drive this task once before believing the rejection. (Retrying the POST
                # alone does not help — the daemon needs a restart.)
                # A 400 used to mean "the daemon has wedged, restart it and try again". With our
                # own engine a refusal is just a refusal - there is no machinery to unstick.
                msg = "The console refused this package"
                if detail:
                    msg += " — %s" % detail
                if risky:
                    msg += ". It may be the file name — try one with no spaces or [ ] brackets."
                print("[install] fail (rejected) name=%r res=%r" % (t.get("name"), res))
                self._set(t, state="error", msg=msg, detail=detail or None, fail_reason="pkg_rejected")
                return
            # A LAN-served file has a byte counter (ours or a peer's /api/served); an external
            # mirror has nothing to measure.
            measurable = bool(t.get("local_progress")) or bool(t.get("progress_url"))
            if measurable:
                self._set(t, state="transferring", msg="Connecting to %s" % bridge.name)
                stalled = 0
                last_mx = -1        # most bytes we have ever seen land, to detect a real stall
                while True:
                    if t["cancel"]:
                        # Past the hand-off. The console owns the bgft job now and this app has
                        # no way to stop it, so the row must not promise otherwise.
                        self._set(t, state="canceled",
                                  msg="Canceled - the console may still finish what it already accepted")
                        return
                    pr = self._progress(t) or {}
                    # Our engine reports its own failures — surface them straight away instead
                    # of sitting through the generic 90s "console isn't pulling" timeout.
                    if pr.get("state") == "error":
                        self._set(t, state="error", fail_reason="engine_error",
                                  msg=(pr.get("msg") or "The install engine stopped without "
                                                        "saying why — try this one again"))
                        return
                    mx, tot = pr.get("max", 0), (pr.get("total") or total or 0)
                    pct = min(90, int(mx * 90 / tot)) if tot else 0
                    self._set(t, pct=pct, msg="Downloading %d%%" % pct)
                    if tot and mx >= tot:
                        break
                    # Stall = no NEW bytes, not "still at 0%". Keying this off pct==0 meant that
                    # once a single percent landed the counter reset forever: a transfer that died
                    # at 3% held this worker — and, because installs are serialised per console,
                    # that console's entire queue — open indefinitely with no way out but a restart.
                    if mx > last_mx:
                        last_mx = mx
                        stalled = 0
                    else:
                        stalled += 1
                    if stalled > 180:      # ~90s with no new bytes at ANY point in the transfer
                        if last_mx > 0:
                            self._set(t, state="error", pct=pct, fail_reason="transfer_stalled",
                                      msg="Transfer stopped at %d%% and nothing more arrived for 90s. "
                                          "The console stopped pulling — start it again from the queue."
                                          % pct)
                        else:
                            # The ELF provably answered - install_spawn only returns ok after the
                            # spawned installer wrote a verdict - so the old wording ("is the ELF
                            # loaded?") blamed the one thing known to be fine. What is true is that
                            # the console accepted the package and has not started pulling it.
                            self._set(t, state="submitted", pct=0,
                                      msg="The console accepted this but has not started pulling it yet — "
                                          "check the %s's Downloads before trying again"
                                          % bridge.kind_label())
                        return
                    time.sleep(0.5)
                # POST-DOWNLOAD: follow the on-console install to playable.
                # bgft.db gives live install %, app.db (installed_titles) is the definitive
                # ready signal. If every byte lands but the title never registers, the console
                # rejected the package (bad/incompatible dump) — report that, don't hang.
                tid = t.get("title_id")
                self._set(t, state="promoting", pct=90, msg="Installing on %s" % bridge.name)
                # 2100 s, strictly above the 1800 s evidence rule below. At 600 s the "row has
                # not moved for 30 minutes" test could never run - the clock fired first and
                # called a still-installing title stale_install, with advice to delete it.
                deadline = time.time() + 2100
                bytes_done_at = None
                last_row, last_row_at = None, None      # [11] evidence of progress, not a stopwatch
                cur, installed = 90, False
                # THE EXPENSIVE READS ON THEIR OWN CLOCK. installed_titles(force=True) is a whole
                # app.db pull plus a scan of four /user/app roots, and installed_app_pkg is up to
                # four more listings; at the 3 s cadence that was ~40 console requests every 3 s
                # for the length of an install, on the accept loop that also serves the UI. The
                # bgft row stays on the 3 s cadence (it is one small file); these run at most
                # every 15 s. The verdict conditions themselves are unchanged.
                heavy_at = 0.0
                # Was it ALREADY registered before we started? If so, "it is in app.db" proves
                # nothing — that is exactly the state where the console refuses to re-pull the data
                # and leaves the old, broken files in place. We then demand proof on disk instead.
                was_registered = bool(t.get("_pre_registered"))
                pkg_bytes = t.get("total") or 0
                is_addon = str(t.get("kind") or "").lower() in ADDON_KINDS
                before = t.get("_bgft_before")
                while time.time() < deadline:
                    if t["cancel"]:
                        self._set(t, state="canceled",
                                  msg="Canceled - the console may still finish what it already accepted")
                        return
                    heavy_due = (time.time() - heavy_at) >= 15.0
                    if heavy_due:
                        heavy_at = time.time()
                    # A PS4 ANSWERS FOR ITSELF, AND EVERYTHING BELOW THIS ASKS A PS5's QUESTIONS.
                    #
                    # The add-on branch waits on install_job_row(), which reads bgft.db - a PS5
                    # database. A PS4 keeps task DIRECTORIES under /user/bgft/task instead, so that
                    # call answers nothing on a PS4 and an update could never be confirmed: measured
                    # with Subnautica's update, which installed completely - patch.pkg, patch.pbm,
                    # patch.json and patch.xml all present at /user/patch/CUSA13529 - while this
                    # queue sat at "Installing 99%" indefinitely.
                    #
                    # Our own payload already reaches the verdict, on the console, using the proof
                    # that suits the package: app.pkg for a game, patch.pkg for an update, ac.pkg for
                    # add-on content - and it errors out rather than hanging if a job stops moving.
                    # So for a PS4 that verdict IS the answer, and asking it costs one request every
                    # fifteen seconds rather than a database read that cannot work.
                    if bridge.is_ps4() and heavy_due:
                        j = bridge.engine_job() or {}
                        jst = (j or {}).get("state")
                        if jst == "installed":
                            installed = True
                            break
                        if jst == "error":
                            self._set(t, state="error", pct=99, fail_reason="console_refused",
                                      msg=(j.get("msg")
                                           or "The console stopped this install. The install log on "
                                              "the PS4 says why"))
                            return
                    if is_addon and not bridge.is_ps4():
                        # An update/DLC adds to a title that is ALREADY registered and already has a
                        # full-size app.pkg, so neither app.db nor app.pkg can say anything about it.
                        # Its own completed bgft row is the proof: a row that is not the one we saw
                        # before handing over, fully transferred, with a terminal status.
                        row = bridge.install_job_row(tid) if tid else None
                        if row and row != before and row[2] and row[1] >= row[2] \
                                and row[0] in BGFT_DONE:
                            installed = True
                            break
                    elif tid and heavy_due and tid in (bridge.installed_titles(force=True) or []):
                        # app.db presence is NOT proof, for a new title either. A metadata-only
                        # registration (sceAppInstUtilAppInstallPkg) puts the title in app.db with
                        # no game data behind it — the tile appears, "Ready to play" is shown, and
                        # launching it crashes the console. The only honest proof is the game's own
                        # app.pkg on disk, at /user/app/<TID>/ or /mnt/ext1/user/app/<TID>/.
                        got = bridge.installed_app_pkg(tid)
                        if got and (not pkg_bytes or got >= int(pkg_bytes * 0.98)):
                            # For a title that was ALREADY registered with a full-size app.pkg
                            # before we started, this test proves nothing — the files it is looking
                            # at can be the previous install's. Demand one piece of fresh evidence:
                            # a bgft row that is not the one we snapshotted at hand-off. A genuinely
                            # new title needs no such proof, because app.pkg appearing at full size
                            # where there was nothing IS the new evidence.
                            if was_registered:
                                row = bridge.install_job_row(tid)
                                if not (row and row != before and row[0] in BGFT_DONE
                                        and row[2] and row[1] >= row[2]):
                                    time.sleep(3)
                                    continue            # keep waiting for OUR install to land
                            installed = True
                            break
                    prog = bridge.install_progress(tid) if tid else None
                    if prog and prog[1]:
                        cur = max(cur, 90 + int(9 * min(1.0, prog[0] / prog[1])))
                        if prog[0] >= prog[1] and bytes_done_at is None:
                            bytes_done_at = time.time()      # all bytes in; registration should follow
                    else:
                        cur = min(99, cur + 1)
                    # A CLOCK IS NOT A VERDICT. This used to fail the job after four minutes with
                    # "Console rejected this package … (bad dump)" — an accusation about the file,
                    # produced by a stopwatch. A large PS5 title can spend well over four minutes
                    # copying and decrypting after the last byte lands, and a brief FTP hiccup makes
                    # installed_titles() return None (read as "not there yet"), so a perfectly good
                    # install was called a bad dump and the user was told to re-dump the game.
                    #
                    # Wait on EVIDENCE instead: keep going while the console's own bgft row is still
                    # in a non-terminal state, and only give up when it reaches a terminal status
                    # that is not success, or when the row stops changing for a long time. Then say
                    # what is actually known — "the console has not finished" — not what is guessed.
                    if bytes_done_at:
                        row_now = None
                        try:
                            row_now = bridge.install_job_row(tid) if tid else None
                        except Exception:
                            row_now = None
                        if row_now and row_now != last_row:
                            last_row, last_row_at = row_now, time.time()   # still moving
                        if row_now and row_now[0] in BGFT_FAILED:
                            self._set(t, state="error", pct=99, fail_reason="bgft_failed",
                                      msg="The console's installer rejected this package "
                                          "(bgft status %s). Nothing here was changed — the file "
                                          "may be an incomplete dump." % row_now[0])
                            return
                        stalled = time.time() - (last_row_at or bytes_done_at)
                        if stalled > 1800:
                            self._set(t, state="error", pct=99, fail_reason="install_stalled",
                                      msg="Every byte reached %s, but its installer has not "
                                          "recorded progress for %d minutes. Check the %s's "
                                          "Downloads notification — nothing here was changed."
                                          % (bridge.name, int(stalled // 60),
                                             bridge.kind_label()))
                            return
                    self._set(t, pct=cur, msg="Installing %d%%" % cur)
                    time.sleep(3)
                # There is no ladder to police any more. This block read the third-party host's
                # own last-install record to find out which rung it had used, because its lower rung
                # (sceAppInstUtilAppInstallPkg) registers metadata with no data behind it and still
                # answers ok:true - a tile on the dashboard that crashes when launched. Our engine
                # makes one call, InstallByPackage, and never falls back; the proof it worked is the
                # bgft row and the files, both checked below.
                if installed:
                    # Nothing to mark dirty and nothing to clear. A third-party host had to be
                    # tracked across installs because it wedged after heavy ones; the spawned
                    # installer exits when it is done and leaves nothing behind.
                    # Last check before calling it done: if the game's own data is visibly short,
                    # say so rather than putting a tile on the dashboard that crashes when launched.
                    # `None` means we simply cannot see it — never fail on that, only on real proof.
                    # Size proof only means something for a base install — for an add-on this reads
                    # the PARENT's app.pkg, which is unrelated to the add-on's size.
                    got = bridge.installed_app_pkg(tid) if (tid and not is_addon) else None
                    if got is not None and pkg_bytes and got < int(pkg_bytes * 0.90):
                        self._set(t, state="error", pct=99, fail_reason="short_install",
                                  msg="Only %s of %s reached the console — delete the title there "
                                      "(Options → Delete) and install it again."
                                      % (human_size(got), human_size(pkg_bytes)))
                        return
                    self._set(t, state="playable", pct=100,
                              msg="Update installed" if is_addon else "Ready to play")
                    self._finished_installing(t)
                    if not is_addon:
                        remember_installed(tid)
                else:
                    # THE CAP WAS HIT WITH NO EVIDENCE EITHER WAY. Everything that IS evidence -
                    # a completed row, a failed row, a row frozen for 30 minutes - returned from
                    # inside the loop. Reaching here means 35 minutes passed and the console never
                    # said yes or no, so say exactly that. This used to fail the job as
                    # stale_install / addon_not_confirmed / "did not confirm" and tell the user to
                    # delete the game - a verdict produced by a stopwatch, about an install that
                    # then frequently completed on its own.
                    print("[install] %s: no verdict from the console after %d minutes - "
                          "install_unconfirmed (addon=%s registered_before=%s)"
                          % (t.get("id"), int((time.time() - t.get("_submitted_at", time.time())) // 60),
                             is_addon, was_registered))
                    if is_addon:
                        # Never tell someone to delete the GAME because its update did not take.
                        self._set(t, state="error", pct=99, fail_reason="install_unconfirmed",
                                  msg="The %s has not confirmed this %s yet — it may still be "
                                      "installing it. Check the %s's Downloads before trying again; "
                                      "the base game was left alone."
                                      % (bridge.kind_label(), t.get("kind") or "add-on",
                                         bridge.kind_label()))
                    elif was_registered:
                        # The likeliest cause is still the documented one - the title was already
                        # registered, so the console kept the old files - but that is a guess
                        # until the PS5 is looked at, so it is offered as the thing to check.
                        self._set(t, state="error", pct=99, fail_reason="install_unconfirmed",
                                  msg="The %s has not confirmed this install yet — it may still be "
                                      "installing. Check the %s: if it only shows the old copy, "
                                      "delete it there (Options → Delete) and install again."
                                      % (bridge.kind_label(), bridge.kind_label()))
                    else:
                        self._set(t, state="error", pct=99, fail_reason="install_unconfirmed",
                                  msg="The %s has not confirmed this install yet — it may still be "
                                      "installing. Check the %s's Downloads before trying again."
                                      % (bridge.kind_label(), bridge.kind_label()))
            else:
                self._set(t, state="submitted", pct=0, simulated=True,
                          msg="Sent to %s (progress not measurable for this source)" % bridge.name)
        else:
            # The console is not reachable, so NOTHING was installed. This used to animate progress
            # to 100%, report "Installed (simulated)" and then call remember_installed() — which
            # writes the title into installed.json permanently, so the library went on showing a
            # game as installed that had never left the PC. A demo animation is not worth a lie
            # about the state of someone's console.
            # HOLD, do not error - and take the rest of this console's queue with it.
            # Ten games queued and the PS5 reboots after the third: each remaining job costs about
            # two seconds to discover the console is gone, so the whole queue used to burn down to
            # `error` in under half a minute and every one of them needed re-queueing by hand. The
            # wedge path has held-and-paused since B8; being switched off is no different, and it
            # is far more likely.
            msg = ("\u23f8 %s is not reachable — nothing was installed. Turn the console on, load "
                   "PKG MUTANT SHOP, then press \u25b6 Start queue." % bridge.name)
            self._set(t, state="held", pct=0, fail_reason="console_offline", msg=msg)
            self._pause_pending(t.get("console"), msg)

    def _run_pc_copy(self, t):
        """Copy a game from another PC into this one's library folder.

        Downloads to a .part file and renames only when the whole thing is here, so an
        interrupted copy can never look like a finished game. Resumes with a Range request
        when a partial is already present."""
        url = t.get("url")
        dest_dir = t.get("dest")
        name = t.get("file") or (t.get("name") or "game") + ".pkg"
        if not url or not dest_dir:
            self._set(t, state="error", msg="There is no file to copy — rescan the library")
            return
        try:
            os.makedirs(dest_dir, exist_ok=True)
        except OSError as e:
            self._set(t, state="error", detail="%s: %s" % (dest_dir, e),
                      msg="Could not create the destination folder — check the drive is "
                          "connected and not read-only")
            return
        final = os.path.join(dest_dir, name)
        part = final + ".part"
        if os.path.exists(final):
            self._set(t, state="playable", pct=100, msg="Already in this library")
            return

        have = os.path.getsize(part) if os.path.exists(part) else 0
        total = t.get("total") or 0
        self._set(t, state="transferring", pct=0, msg="Copying from %s…" % (t.get("from_pc") or "the other PC"))
        try:
            req = urllib.request.Request(url)
            if have:
                req.add_header("Range", "bytes=%d-" % have)
            with urllib.request.urlopen(req, timeout=30) as r:
                if r.status == 200:
                    have = 0                      # server ignored the range: start over
                clen = int(r.headers.get("Content-Length") or 0)
                if not total:
                    total = have + clen
                mode = "ab" if have else "wb"
                last = 0
                with open(part, mode) as f:
                    while True:
                        if t["cancel"]:
                            self._set(t, state="canceled", msg="Canceled — partial file kept for resume")
                            return
                        chunk = r.read(1 << 20)
                        if not chunk:
                            break
                        f.write(chunk)
                        have += len(chunk)
                        if total and have - last > (4 << 20):
                            last = have
                            self._set(t, pct=min(99, int(have * 100 / total)),
                                      msg="Copying %s / %s" % (human_size(have), human_size(total)))
        except Exception as e:
            self._set(t, state="error", detail=str(e)[:300],
                      msg="The copy stopped — %s" % str(e)[:90])
            return

        if total and os.path.getsize(part) != total:
            self._set(t, state="error",
                      msg="Only %s of %s copied — kept so it can resume, press retry"
                          % (human_size(os.path.getsize(part)), human_size(total)))
            return
        try:
            os.replace(part, final)
        except OSError as e:
            self._set(t, state="error", detail=str(e)[:300],
                      msg="The copy finished but could not be renamed — something else may "
                          "have the file open")
            return
        self._set(t, state="playable", pct=100, msg="Copied into this library")
        try:
            self.library.scan()                   # it is a real local game now
        except Exception:
            pass

    def _run_console_local(self, t):
        """CONSOLE-LOCAL lane: the package is already on the PS5 (USB stick, external drive).
        Nothing is transferred from here - the console serves the file to its own install
        daemon, which is the same pipeline a downloaded game goes through. We start it and
        follow the console's status until it finishes."""
        bridge = self.fleet.bridge(t["console"])
        if bridge is None:
            self._set(t, state="error", msg="No console configured")
            return
        path = t.get("local_path")
        if not path:
            self._set(t, state="error", msg="This title has no package file to install")
            return
        # Snapshot the console BEFORE the handoff, for the same reason the download lane does:
        # a title that is already registered, or an old completed bgft row from a previous install,
        # must not be mistaken afterwards for proof that THIS package landed.
        tid = t.get("title_id")
        try:
            t["_pre_registered"] = bool(tid) and tid in (bridge.installed_titles(force=True) or [])
        except Exception:
            t["_pre_registered"] = False
        try:
            t["_bgft_before"] = bridge.install_job_row(tid) if tid else None
        except Exception:
            t["_bgft_before"] = None
        self._set(t, state="submitting", pct=0, msg="Handing the package to the console…")
        try:
            res = bridge.install_local_on_console(path, t.get("name") or "") or {}
        except ShopHTTPError as e:
            # The console ANSWERED, with a status and usually a sentence. Relaying it beats the
            # "did not answer - load it again" advice below, which is wrong for a console that
            # is plainly up and has just said no.
            self._set(t, state="error", detail=e.body[:300],
                      msg=e.sentence("The console refused the package"))
            return
        except Exception as e:
            self._set(t, state="error", detail=str(e)[:300],
                      msg="PKG MUTANT SHOP on the %s did not answer — load it again%s"
                          % (bridge.kind_label(), bridge.reload_suffix()))
            return
        if not res.get("ok"):
            self._set(t, state="error",
                      msg=res.get("error") or "The console refused the package")
            return
        # The console installs on its own thread; poll it rather than holding this worker
        # on a socket for the whole install.
        self._set(t, state="promoting", pct=5, msg="Installing on the console…")
        deadline = time.time() + 3600
        while time.time() < deadline:
            if t["cancel"]:
                self._set(t, state="canceled", msg="Canceled (the console may still finish)")
                return
            time.sleep(4)
            try:
                st = bridge._shop("/api/install/status", timeout=15) or {}
            except Exception:
                continue
            if st.get("done"):
                if not st.get("ok"):
                    self._set(t, state="error",
                              msg=st.get("message") or "The install did not finish — press "
                                                       "retry to hand it over again")
                    return
                if not st.get("accepted_only"):
                    # The console did the install in-process and has really finished.
                    self._set(t, state="playable", pct=100, msg="Installed from the drive")
                    self._finished_installing(t)
                    return
                # etaHEN only ACCEPTED it. "SUCCESS: Task started" arrives in about a second and
                # the install then runs for minutes and can still fail. Declaring it playable here
                # is the same lie the download lane stopped telling in 3.18.2, so wait for the
                # console own proof instead.
                self._confirm_console_local(t, bridge)
                return
            if st.get("active") and t["pct"] < 95:
                self._set(t, pct=min(95, t["pct"] + 2), msg=st.get("message") or "Installing…")
        self._set(t, state="error",
                  msg="The console never reported this one finished. It may still be installing "
                      "— check the %s before trying again" % bridge.kind_label())

    def _confirm_console_local(self, t, bridge):
        """Wait for real proof that a package etaHEN merely ACCEPTED has actually installed.

        Same evidence the download lane uses, because it is the only evidence that does not lie:
          * add-on / already-registered title -> a bgft row that is NOT the one we snapshotted,
            fully transferred, with a terminal status (BGFT_DONE);
          * a brand-new title -> the game own app.pkg present on disk at full size.
        A timeout is reported as a timeout, never as success and never as a bad package.
        """
        tid = t.get("title_id")
        before = t.get("_bgft_before")
        was_registered = bool(t.get("_pre_registered"))
        kind = (t.get("kind") or "base").lower()
        is_addon = kind in ("update", "patch", "dlc", "backport") or was_registered
        pkg_bytes = int(t.get("total") or 0)
        self._set(t, state="promoting", pct=90,
                  msg="Accepted by the console — waiting for it to finish installing…")
        deadline = time.time() + 3600
        if not tid:
            # Without a title id there is nothing to look for. Say so rather than inventing a
            # verdict: the package was handed over and the console is dealing with it.
            self._set(t, state="submitted", pct=0,
                      msg="Handed to the console — no title id, so this app cannot confirm the "
                          "finish. Check the PS5 home screen.")
            return
        while time.time() < deadline:
            if t["cancel"]:
                self._set(t, state="canceled",
                          msg="Canceled — the console may still finish what it accepted")
                return
            time.sleep(5)
            row = None
            try:
                row = bridge.install_job_row(tid)
            except Exception:
                pass
            fresh_done = bool(row and row != before and row[2] and row[1] >= row[2]
                              and row[0] in BGFT_DONE)
            if fresh_done:
                self._set(t, state="playable", pct=100, msg="Installed from the drive")
                self._finished_installing(t)
                return
            if not is_addon:
                try:
                    got = bridge.installed_app_pkg(tid)
                except Exception:
                    got = None
                if got and (not pkg_bytes or got >= int(pkg_bytes * 0.98)):
                    self._set(t, state="playable", pct=100, msg="Installed from the drive")
                    self._finished_installing(t)
                    return
            if row and row[2]:
                self._set(t, pct=min(99, 90 + int(9 * min(1.0, row[1] / float(row[2])))),
                          msg="Installing on the console…")
        self._set(t, state="error", pct=99, fail_reason="local_not_confirmed",
                  msg="The console accepted this package but never recorded it as installed. "
                      "Check the PS5 notifications — nothing here was changed.")

    def _run_peer_delegate(self, t):
        """The PC that owns the backup is sending it to the console. Mirror its job here.

        The transfer happens machine-to-machine without passing through this one, so there is nothing
        to do but reflect it — the user asked for the game on the PS5 and should watch it go there,
        not have to open the app on the other PC to find out how it is doing."""
        owner = t.get("owner_url")
        who = t.get("from_pc") or "the other PC"
        if not owner:
            self._set(t, state="error",
                      msg="This game is no longer where the library said it was — rescan")
            return

        # Start it on the other machine NOW. This happens when THIS task runs, so pressing Start
        # here starts everything — the other PC is never left holding a job waiting to be released,
        # and it never matters which device the button was pressed on.
        oid = t.get("owner_job")
        if not oid:
            self._set(t, state="submitting", pct=0, msg="Asking %s to send it…" % who)
            payload = {"install_key": t.get("owner_key"), "drive": t.get("drive") or "ext1",
                       "title_id": t.get("title_id"), "name": t.get("name"),
                       "mode": "now"}          # always immediate: our queue already decided the wait
            try:
                rq = urllib.request.Request(
                    owner + "/api/install", method="POST",
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"})
                with urllib.request.urlopen(rq, timeout=25) as rr:
                    ans = json.loads(rr.read().decode("utf-8", "replace"))
            except Exception as e:
                ans = {"ok": False, "error": str(e)}
            oid = (ans.get("ids") or [None])[0] if isinstance(ans, dict) else None
            if not (isinstance(ans, dict) and ans.get("ok") and oid):
                # It could not take it (asleep, busy, older build). Do the whole thing ourselves
                # rather than stopping: pull the file here, then send it on. Slower, still automatic.
                print("[install] %s could not take the mount (%s) - doing it here instead"
                      % (who, (ans or {}).get("error")))
                self._set(t, msg="%s did not answer — copying it here first" % who)
                t["lane"] = "peer-mount"
                t["url"] = (t.get("owner_url") or "") + "/library/" + str(t.get("owner_key") or "")
                t["file"] = unquote(str(t.get("owner_key") or "")) or (t.get("name") or "game")
                plat = "PS5" if str(t.get("title_id") or "").upper().startswith("PPSA") else "PS4"
                root = self.cfg.get("library", {}).get("root") or LIBRARY_ROOT
                t["dest"] = os.path.join(root, plat if plat in LIBRARY_LAYOUT else "")
                t["mount_dest"] = mount_dest_for_drive(self.cfg, t.get("drive"))
                return self._run_peer_mount(t)
            t["owner_job"] = oid
        self._set(t, state="transferring", pct=0, msg="%s is sending it to the console…" % who)
        misses = 0
        while True:
            if t["cancel"]:
                try:
                    urllib.request.urlopen(owner + "/api/queue/" + quote(str(oid)) + "/cancel",
                                           data=b"{}", timeout=8).read()
                except Exception:
                    pass
                self._set(t, state="canceled", msg="Canceled on %s" % who)
                return
            doc = http_get_json(owner + "/api/queue", timeout=8)
            row = None
            for x in ((doc or {}).get("tasks") or []):
                if str(x.get("id")) == str(oid):
                    row = x
                    break
            if row is None:
                misses += 1
                if misses > 20:          # cleared from its queue, or the PC went away
                    self._set(t, state="error", msg="%s stopped reporting this job" % who)
                    return
                time.sleep(3)
                continue
            misses = 0
            st, pct = row.get("state"), int(row.get("pct") or 0)
            msg = row.get("msg") or ""
            if st in ("playable", "error", "canceled"):
                self._set(t, state=st, pct=100 if st == "playable" else pct,
                          msg=("%s — sent from %s" % (msg, who)) if msg else st)
                if st == "playable":
                    self._finished_installing(t)
                    remember_installed(t.get("title_id"))
                return
            # It was submitted with mode "now", so it is never waiting to be released. If it is
            # briefly "queued" that is only its worker picking it up, or it was held by something
            # else — release it rather than reporting a dead end the user has to go and fix.
            if st == "held":
                try:
                    urllib.request.urlopen(owner + "/api/queue/start", data=b"{}", timeout=8).read()
                except Exception:
                    pass
            self._set(t, state="promoting" if st == "promoting" else "transferring",
                      pct=pct,
                      msg=("Starting on %s…" % who) if st in ("held", "queued")
                          else "%s (on %s)" % (msg or "Sending…", who))
            time.sleep(2)

    def _run_peer_mount(self, t):
        """A backup that lives on ANOTHER PC, going to the console's ShadowMount folder.

        Two steps that already work, run in order and nothing else: pull the container into this
        library exactly as a PC-to-PC copy does, then send it on exactly as a local backup does.
        Neither of those two methods is touched — a failure in either reports itself and stops here.
        """
        self._run_pc_copy(t)                       # step 1: it is a local file afterwards
        if t.get("cancel") or t.get("state") != "playable":
            return                                 # the copy already said what went wrong
        final = os.path.join(t.get("dest") or "", t.get("file") or "")
        if not os.path.isfile(final):
            self._set(t, state="error", detail=final,
                      msg="The copy finished but the file is not where it should be — rescan "
                          "the library")
            return
        t["local_path"] = final
        t["total"] = os.path.getsize(final)
        t["lane"] = "mount"                        # step 2 behaves as any local backup does
        t["dest"] = t.get("mount_dest") or mount_dest_for_drive(self.cfg, t.get("drive"))
        self._set(t, state="transferring", pct=0,
                  msg="Copied from %s — sending to the console…" % (t.get("from_pc") or "the other PC"))
        self._run_mount(t)

    def _run_mount(self, t):
        """MOUNT lane: get the backup into the chosen drive's ShadowMount folder; it mounts itself.

        A PS5 backup is never installed - there is no PKG and no BGFT. ShadowMount watches
        /mnt/<drive>/homebrew and mounts what turns up, so the whole lane is: put the container in
        the right folder under its real name, then wait for the console to say it mounted.

        Two transports, same ending. The console PULLS it with our own downloader whenever it can
        (see fetch_to) - that is one hop even when the file lives on another PC. The PC pushes it
        only for a folder-shaped game or a console running an ELF older than /api/engine/fetch.
        """
        bridge = self.fleet.bridge(t["console"])
        if bridge is None:
            self._set(t, state="error", msg="No console configured")
            return
        local = t.get("local_path")
        dest = t.get("dest") or mount_dest_for_drive(self.cfg, t.get("drive"))
        # A PULL needs no local file - the console fetches it from whichever PC serves it, which may
        # not be this one. Only the push fallback needs something on this disk.
        have_local = bool(local and os.path.exists(local))
        if not have_local and not t.get("url"):
            self._set(t, state="error",
                      msg="The file for this backup is missing — rescan the library")
            return
        is_dir = have_local and os.path.isdir(local)   # a game stored as a FOLDER is equally valid
        total = t.get("total") or ((0 if is_dir else os.path.getsize(local)) if have_local else 0)
        name = (os.path.basename(local.rstrip("\\/")) if have_local
                else (t.get("remote_name") or "backup.ffpfsc"))
        # Check the console can actually REGISTER a new title before spending an hour sending one.
        # Delivering the container was never the hard part; registering it is, and when ShadowMount
        # is on the wrong register route the game mounts and then fails — after the whole transfer.
        # See Ps5Bridge.register_path() for the full story. Fixing the config is free; it only
        # matters for titles the console does not already know, so a restart is skipped when the
        # title is already registered or a game is running.
        if t.get("lane") == "mount":
            try:
                res = bridge.ensure_register_path()
                if res == "fixed":
                    self._set(t, msg="Fixing ShadowMount's register path…")
                    r = bridge.restart_shadowmount()
                    print("[mount] register path fixed on %s; ShadowMount %s" % (bridge.name, r))
                    if r == "busy":
                        self._set(t, msg="Register path fixed — takes effect once a game is not running")
            except Exception as e:
                print("[mount] register-path preflight skipped: %r" % e)
        # The container keeps its own name: ShadowMount identifies the title from it. (A "sendpkg"
        # lane that renamed to <titleid>.pkg used to be tested here; no lane has had that name
        # for a long time.)
        upname = name
        try:
            # Through OUR on-console file API, with the third-party FTP only as fs_*'s fallback.
            # Until 3.24.4 this lane dialled `ftp_port` straight from config, so on a console
            # running etaHEN (FTP on 1337, 2121 closed) it could not connect at all - and answered
            # that by animating a fake transfer to 100%.
            if not bridge.fs_mkdir(dest):
                raise IOError("could not create %s on the console" % dest)
        except Exception as e:
            # NOTHING was transferred, so the backup is not on the console. This used to answer an
            # unreachable FTP by animating the bar to 100% and reporting "Deployed to ShadowMount
            # (simulated)" in state `playable` - the exact lie the install lane had already removed
            # (see _run's console_offline branch): a finished-looking green job for a game that
            # never left the PC. Say what actually happened instead.
            self._set(t, state="error", pct=0, fail_reason="console_unreachable",
                      msg="Could not reach %s to prepare %s — nothing was sent. Load "
                          "PKG MUTANT SHOP on the PS5, then press ▶ Start queue again."
                          % (bridge.name, dest))
            print("[mount] console unreachable (%s): %r" % (bridge.ip, e))
            return
        # ---------------------------------------------------------------------------------
        # PREFERRED: THE CONSOLE PULLS IT, using our own downloader.
        #
        # Only a single-file container can be pulled - a game stored as a folder is a tree and has
        # to be walked, which is what the push below does. And we need a URL the console can reach:
        # `url` is already set when the backup lives on a peer, otherwise we serve it ourselves.
        # ---------------------------------------------------------------------------------
        pull_url = t.get("url") or ""
        if not pull_url and t.get("key"):
            pull_url = "http://%s:%d/library/%s" % (companion_ip_for(bridge.ip),
                                                    self.cfg["companion"]["port"], t["key"])
        if not is_dir and pull_url:
            okf, info = bridge.fetch_to(pull_url, dest, upname, size=total)
            if okf:
                self._set(t, state="transferring", pct=0,
                          msg="%s is downloading it to %s…" % (bridge.name, dest))
                stalled, last_done = 0, -1
                while True:
                    if t["cancel"]:
                        # Tell the console to stop PULLING. It exposes /api/engine/cancel for
                        # exactly this (server.c: sets g_job.cancel, and its download loop stops
                        # at the next chunk); without the call the row read "Canceled" while the
                        # console finished the download, renamed the .part and ShadowMount mounted
                        # the game the user had just cancelled - and the next backup was refused
                        # with "a download is already running". Best effort: the row is cancelled
                        # either way.
                        try:
                            bridge._shop("/api/engine/cancel", timeout=8)
                        except Exception as e:
                            print("[mount] could not ask the console to stop the download: %r" % e)
                        self._set(t, state="canceled",
                                  msg="Canceled - the console was asked to stop the download")
                        return
                    j = bridge.engine_job() or {}
                    # Pin the job id. dl_job_t carries one precisely so a caller "can never mistake
                    # the PREVIOUS job's done for its own" - and this loop starts by polling, so
                    # without it a stale "done" from the last install reads as instant success.
                    if info.get("job_id") is not None and j.get("job_id") not in (None, info["job_id"]):
                        self._set(t, state="error", pct=0, fail_reason="transfer_stalled",
                                  msg="Another download started on the console and replaced this "
                                      "one. Try it again on its own.")
                        return
                    st = str(j.get("state") or "")
                    done, tot = int(j.get("done") or 0), int(j.get("total") or total or 0)
                    if st in ("error", "failed"):
                        self._set(t, state="error", pct=0, fail_reason="transfer_stalled",
                                  msg=j.get("msg") or "The console could not download the backup")
                        return
                    if st in ("done", "installed"):
                        break
                    pct = min(99, int(done * 99 / tot)) if tot else 0
                    self._set(t, pct=pct,
                              msg="Downloading to the console %d%% (%s)" % (pct, human_size(done)))
                    # A pull that stops moving is the console's problem, not ours - say so rather
                    # than sitting on a frozen bar. 5 minutes of no new bytes is generous for LAN.
                    stalled = 0 if done != last_done else stalled + 1
                    last_done = done
                    if stalled > 150:
                        self._set(t, state="error", pct=pct, fail_reason="transfer_stalled",
                                  msg="The console stopped downloading at %d%%. Check the drive is "
                                      "connected and this PC is still sharing the file." % pct)
                        return
                    time.sleep(2)
                self._set(t, pct=99, msg="Downloaded — waiting for the backup service")
                return self._mount_confirm(t, bridge, dest)
            if not info.get("unsupported"):
                # A real refusal - a missing drive, a full one, or a download already running.
                # Falling back to another transport would only hide it.
                self._set(t, state="error", pct=0, fail_reason="console_unreachable",
                          msg=info.get("error") or "The console would not take this backup")
                return
            # This console predates /api/engine/fetch. If the file lives on another PC, that PC can
            # still push it the old way; if it is here, we push it ourselves below.
            if not have_local and t.get("owner_url") and t.get("owner_key"):
                print("[mount] this console has no /api/engine/fetch - delegating to %s"
                      % (t.get("from_pc") or "the other PC"))
                t["lane"] = "peer-delegate"
                return self._run_peer_delegate(t)
            if not have_local:
                self._set(t, state="error", pct=0, fail_reason="console_unreachable",
                          msg="This console is running an older PKG MUTANT SHOP that cannot "
                              "download backups itself. Load the current one from Payload Manager.")
                return
            print("[mount] this console has no /api/engine/fetch - falling back to pushing")

        if not have_local:
            self._set(t, state="error", pct=0, fail_reason="console_unreachable",
                      msg="This backup is on another PC and the console could not fetch it.")
            return
        self._set(t, state="transferring", msg="Uploading backup → %s…" % dest)
        try:
            sent = [0]
            # Bytes already delivered by EARLIER files of a folder-shaped game, so the bar reads
            # against the whole folder instead of climbing a few percent and resetting per file.
            done_before = [0]
            last_pct = [-1]

            def cb(_chunk_len, _running):
                # Raising here is how cancel unwinds a transfer that is already in flight: the
                # exception propagates out of the request, through fs_write, to the handler below.
                if t["cancel"]:
                    raise _Cancelled()
                sent[0] = done_before[0] + _running
                pct = min(99, int(sent[0] * 100 / total)) if total else 0
                # urllib hands the body over in 8 KB blocks - ~11.8 million callbacks for a 90 GB
                # push - so only take the queue lock when there is a new percent to show.
                if pct != last_pct[0]:
                    last_pct[0] = pct
                    self._set(t, pct=pct, msg="Uploading %d%%" % pct)

            def push(local_path, remote_path):
                size = os.path.getsize(local_path)
                with open(local_path, "rb") as f:
                    ok = bridge.fs_write(remote_path, _ProgressReader(f, cb),
                                         timeout=3600, size=size)
                if not ok:
                    raise IOError("the console rejected %s" % remote_path)
                done_before[0] += size

            if os.path.isdir(local):
                # A game stored as a FOLDER: same destination, same progress, but the tree has to be
                # recreated file by file. Kept as its own branch so the single-file push below — the
                # path every container has always taken — is not altered.
                base = os.path.basename(local.rstrip("\\/"))
                for dp, _dn, fns in os.walk(local):
                    rel = os.path.relpath(dp, local).replace("\\", "/")
                    remote = "%s/%s" % (dest, base) if rel in (".", "") else "%s/%s/%s" % (dest, base, rel)
                    bridge.fs_mkdir(remote)
                    for fn2 in fns:
                        if t["cancel"]:
                            raise _Cancelled()
                        push(os.path.join(dp, fn2), "%s/%s" % (remote, fn2))
            else:
                # Both transports write <name>.part and rename only once every byte is across, so
                # an interrupted transfer can never leave a partial container in ShadowMount's scan
                # folder looking like a real game.
                push(local, "%s/%s" % (dest, upname))
                if total and sent[0] < total:
                    raise IOError("sent %s of %s" % (human_size(sent[0]), human_size(total)))
        except _Cancelled:
            self._set(t, state="canceled", msg="Canceled")
            return
        except Exception as e:
            # fs_write owns its own transport and closes it; there is no socket to leak here.
            self._set(t, state="error", detail=str(e)[:300],
                      msg="The upload stopped — %s" % str(e)[:90])
            return
        return self._mount_confirm(t, bridge, dest)

    def _mount_confirm(self, t, bridge, dest):
        """The container is in ShadowMount's folder. Wait for the console to actually mount it.

        Shared by both transports - the console-pull and the PC-push end in exactly the same place,
        and this used to be inline in the push path only.

        Delivering the file is NOT the same as the game working: ShadowMount still has to mount the
        image AND register the title, and the register step can fail on its own (its
        "AppInstallTitleDir bridge unavailable", which needs a console restart to clear). Reporting
        "Deployed" regardless is what put games on the dashboard that crash when launched.
        """
        self._set(t, state="promoting", pct=99, msg="Mounting on %s" % bridge.name)
        tid = t.get("title_id")
        if tid:
            ok = False
            deadline = time.time() + 180
            while time.time() < deadline:
                if t["cancel"]:
                    self._set(t, state="canceled", msg="Canceled")
                    return
                if bridge.title_is_mounted(tid):
                    ok = True
                    break
                time.sleep(5)
            if not ok:
                self._set(t, state="error", pct=99, fail_reason="mount_not_registered",
                          msg="The backup is on %s, but the PS5 did not finish mounting it. "
                              "ShadowMount can mount the image and still fail to register the "
                              "title — restart the console, then it will pick it up from the same "
                              "file." % dest)
                return
        else:
            # No title id means nothing to look for, so "ready to play" would be a claim with no
            # evidence behind it. Say what is known: the file is in the folder the backup service
            # watches.
            self._set(t, state="playable", pct=100,
                      msg="Delivered to the console's backup folder — it mounts on its own. "
                          "Check the PS5 home screen.")
            self._finished_installing(t)
            return
        self._set(t, state="playable", pct=100, msg="Mounted — ready to play")
        self._finished_installing(t)
        remember_installed(t.get("title_id"))

    # Queue._confirm() lived here: an app.db-presence poll with no callers. app.db presence is
    # never proof of an install (see the confirm loop in _run), so nothing should call it again.


# installed.json and hashes.json are read-modify-write from several threads at once: the queue
# marks a title installed while the library scan is hashing and the UI is reading. Without a lock,
# two workers finishing together each read the same list and the second write loses the first
# title - the badge reverts and the game looks uninstalled. The re-read happens INSIDE the lock so
# the window between read and write is closed, not merely narrowed.
_state_lock = threading.RLock()

# /api/hash/ runs on the request thread. Files past this size are refused rather than read for
# minutes; a path being hashed is remembered so a second click does not start a second full read.
_HASH_MAX_BYTES = 8 << 30
_HASH_LOCK = threading.Lock()
_HASHING = set()


def remember_installed(title_id):
    if not title_id:
        return
    with _state_lock:
        try:
            data = {"installed": []}
            if os.path.exists(INSTALLED_PATH):
                # Never assume the shape, and never let an unreadable file swallow THIS write:
                # a truncated or hand-edited file used to raise inside the outer try, so the title
                # being recorded was simply lost. Start fresh instead and keep going.
                try:
                    with open(INSTALLED_PATH) as f:
                        loaded = json.load(f)
                    if isinstance(loaded, dict) and isinstance(loaded.get("installed"), list):
                        data = loaded
                    elif isinstance(loaded, list):
                        data = {"installed": loaded}
                    else:
                        print("[installed] %s had an unexpected shape - starting a fresh list"
                              % INSTALLED_PATH)
                except Exception as le:
                    print("[installed] %s is unreadable (%s) - starting a fresh list"
                          % (INSTALLED_PATH, le))
            # Write only when the list changes. A title that is already remembered used to
            # rewrite the file (mkstemp + replace beside the exe) for nothing.
            if title_id not in data["installed"]:
                data["installed"].append(title_id)
                _atomic_write_json(INSTALLED_PATH, data)
        except Exception as e:
            print("[installed] %s" % e)


def local_installed():
    try:
        with _state_lock, open(INSTALLED_PATH) as f:
            loaded = json.load(f)
        if isinstance(loaded, list):
            return loaded
        return loaded.get("installed", []) if isinstance(loaded, dict) else []
    except Exception:
        return []


def prune_local_installed(console_ids):
    """Self-heal: drop remembered installs the console no longer has (kills stale 'ghost' badges like a
    game that was queued once but isn't really on the PS5). Only call when the console list is real.

    Under _state_lock, like remember_installed(): the two are read-modify-write on the same file
    from different threads (this one from every /api/installed poll, that one from a finishing
    worker), and the unlocked write here could drop a title the worker had just recorded. And
    only written when something actually changed - /api/installed is polled on every library
    load, and each poll rewrote the file beside the exe even when the list was identical."""
    with _state_lock:
        have = local_installed()
        keep = sorted(set(have) & set(console_ids))
        if keep != sorted(set(have)):
            _atomic_write_json(INSTALLED_PATH, {"installed": keep})
    return keep


# --------------------------------------------------------------------------- #
# HTTP handler                                                                  #
# --------------------------------------------------------------------------- #
def _exc_line(e):
    """One line for pms.log: the exception and where it was raised. A full traceback in a log
    that installs are diagnosed from is noise; the type, the message and the last frame are
    what anyone needs."""
    try:
        fr = traceback.extract_tb(e.__traceback__)[-1]
        return "%s: %s (at %s:%d in %s)" % (type(e).__name__, str(e)[:160],
                                             os.path.basename(fr.filename), fr.lineno, fr.name)
    except Exception:
        return "%s: %s" % (type(e).__name__, str(e)[:160])


class Handler(BaseHTTPRequestHandler):
    server_version = "PkgMutantShop/" + VERSION
    # Per-connection socket timeout. The stdlib default is None: a client that connected and
    # never sent a request line (a browser preconnect, a phone's half-open Wi-Fi socket) pinned
    # a handler thread for ever. 300 s is long enough that a paused PKG read is never cut - the
    # console pulls in bursts - and short enough to reap the idle ones. HTTP/1.0 stays: one
    # request per connection, deliberately (see protocol_version's default).
    timeout = 300

    def end_headers(self):
        # Remembered so the exception guard below knows whether it may still answer.
        self._headers_done = True
        BaseHTTPRequestHandler.end_headers(self)

    def _guarded(self, fn):
        """Run a request body and turn an escaped exception into a one-line pms.log entry and a
        500 JSON answer - when nothing has been sent yet.

        Without this the exception reached socketserver.handle_error, which prints to stderr;
        the frozen exe has no stderr (--noconsole), so it vanished, and the client got a closed
        socket with zero bytes - which the UI's api() cannot tell apart from "the PC is off".
        A non-numeric Content-Length, a JSON body that is a list, a file that vanished between
        isfile() and open(): each was a silent hang-up."""
        try:
            return fn()
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            raise                                   # the client went away; nothing to answer
        except Exception as e:
            print("[http] %s %s could not be answered: %s" % (self.command, self.path, _exc_line(e)))
            if getattr(self, "_headers_done", False) or getattr(self, "_json_muted", False):
                return None
            try:
                self._json({"ok": False,
                            "error": "This request could not be completed on the PC - "
                                     "pms.log beside the app says why"}, 500)
            except Exception:
                pass

    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")

    def _json(self, obj, code=200):
        # _install() recurses into itself once per part of a multi-part release, and every one of
        # those calls used to reach here and write a complete HTTP response into the SAME socket.
        # A 3-part release wrote four responses to one request; the client reads the first and the
        # rest are garbage on the wire. Muting lets the recursion collect its results and lets the
        # outermost call emit exactly one response.
        if getattr(self, "_json_muted", False):
            return (obj, code)
        payload = json.dumps(obj).encode("utf-8")
        # /api/library is ~200 KB and it crosses the real LAN: the PS5 loads the page shell from
        # the console over loopback, but every /api/* call goes to a PC companion. Measured
        # 199,546 -> 19,990 bytes, a 10x reduction, for the payload fetched on boot and on every
        # refresh. Only when the client asks, and only when it is worth the CPU; images are
        # already compressed and are never routed through here.
        enc = None
        if len(payload) > 4096 and "gzip" in (self.headers.get("Accept-Encoding") or "").lower():
            try:
                payload, enc = gzip.compress(payload, 6), "gzip"
            except Exception:
                enc = None                      # never fail a response over compression
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("Content-Length", str(len(payload)))
        self._cors()
        self.end_headers()
        self.wfile.write(payload)

    def _plain(self, text, code=200):
        data = text.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _body(self):
        """The JSON body as a dict - and {} for anything else. A non-numeric Content-Length
        raised ValueError and a body that parsed to a list or a string reached the routes as
        something without .get(); both were a closed socket with no answer."""
        try:
            n = int(self.headers.get("Content-Length", 0) or 0)
        except (TypeError, ValueError):
            return {}
        if n <= 0:
            return {}
        try:
            j = json.loads(self.rfile.read(n).decode("utf-8", "replace"))
        except ValueError:
            return {}
        return j if isinstance(j, dict) else {}

    def log_message(self, fmt, *args):
        pass

    def do_OPTIONS(self):
        self.send_response(204)
        self._cors()
        # The console-served page calls this PC cross-origin with a JSON Content-Type, so every
        # POST paid a preflight round trip; letting the browser keep the answer for ten minutes
        # halves the cost of each button press from the PS5.
        self.send_header("Access-Control-Max-Age", "600")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _empty(self, code):
        """A header-only answer (HEAD, or a status with no body)."""
        self.send_response(code)
        self.send_header("Content-Length", "0")
        self._cors()
        self.end_headers()

    def do_HEAD(self):
        """Some install clients HEAD the PKG (for size / Accept-Ranges) before GETting it.

        Every other path answers what GET would: the fall-through here used to be an
        unconditional 200, so HEAD /does-not-exist and HEAD /thumb/<no-art> both said "it
        exists" while GET said 404. /library/ and /icon/ are byte-for-byte what they were."""
        srv = self.server
        path = urlparse(self.path).path
        if path.startswith("/library/"):
            raw = unquote(path[len("/library/"):]).split("?")[0]
            rk = registry_key(srv.library.file_registry, raw)
            if not rk or not os.path.isfile(srv.library.file_registry[rk]):
                self.send_response(404)
                self._cors()
                self.end_headers()
                return
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(os.path.getsize(srv.library.file_registry[rk])))
            # Validators, so a resumed copy can tell the file it is appending to is the one it
            # started on. Additive; the download managers that only read the two above are fine.
            for k, v in self._file_validators(srv.library.file_registry[rk]):
                self.send_header(k, v)
            self._cors()
            self.end_headers()
            return
        if path.startswith("/icon/"):
            full = os.path.join(ICON_DIR, os.path.basename(unquote(path[len("/icon/"):]).split("?")[0]))
            ok = os.path.isfile(full)
            self.send_response(200 if ok else 404)
            if ok:
                self.send_header("Content-Type", "image/png")
                self.send_header("Content-Length", str(os.path.getsize(full)))
            self._cors()
            self.end_headers()
            return
        if path.startswith("/thumb/"):
            # GET generates a thumb from the cached icon, or falls back to the icon itself; HEAD
            # must not do that work. "Exists" here means a thumb OR its source icon is on disk.
            base = os.path.basename(unquote(path[len("/thumb/"):]).split("?")[0])
            tid = re.sub(r"\.(webp|png|jpg)$", "", base, flags=re.I)
            have = (os.path.isfile(os.path.join(THUMB_DIR, tid + ".webp"))
                    or os.path.isfile(os.path.join(ICON_DIR, tid + ".png")))
            return self._empty(200 if have else 404)
        if path.startswith("/api/"):
            # Every /api/ route answers 200 to HEAD, as it did before the fall-through was
            # tightened. A hand-picked allowlist here 404'd a dozen routes GET serves (engine/log,
            # move/status, ps5-log, discover, ...) - the HEAD/GET disagreement in the other
            # direction - and the console answers any unknown /api/ with 200 {} too.
            return self._empty(200)
        rel = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        full, err = self._static_resolve(rel)
        if full is None:
            return self._empty(err[0])
        self.send_response(200)
        self.send_header("Content-Type", guess_type(full))
        try:
            self.send_header("Content-Length", str(os.path.getsize(full)))
        except OSError:
            pass
        self._cors()
        self.end_headers()

    def do_GET(self):
        return self._guarded(self._do_GET)

    def _do_GET(self):
        u = urlparse(self.path)
        path, srv = u.path, self.server
        q = parse_qs(u.query)
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/library/"):
            return self._serve_library(unquote(path[len("/library/"):]))
        if path.startswith("/thumb/"):
            return self._serve_thumb(unquote(path[len("/thumb/"):]))
        if path.startswith("/icon/"):
            return self._serve_icon(unquote(path[len("/icon/"):]))
        if path.startswith("/api/served/"):
            raw = unquote(path[len("/api/served/"):])
            # WHICH CONSOLE'S BYTES. _serve_library keeps two counters - one per requesting console
            # address and one shared by-key aggregate - and this route could only ever answer the
            # aggregate. So a peer PC serving the same package to two consoles at once told both of
            # them whichever transfer was further along. Our own jobs read the per-address bucket
            # out of the dict directly (Queue._progress); a job pulling from a PEER has to ask, and
            # ?console=<ip> is how it asks. An ADDRESS rather than a console id, deliberately: ids
            # live in each companion's own config.json and mean nothing on the machine being asked,
            # while the address is what that machine saw connect. A caller that names no console,
            # or names one that has not been sent a byte yet, still gets the aggregate - the exact
            # answer this route has always given - so an older companion on either end, and a newer
            # one asked before the first byte lands, all behave as they do today.
            _who = (q.get("console") or [""])[0].strip()
            if _who:
                for _k in ("%s|%s" % (raw, _who), "%s|%s" % (quote(raw), _who)):
                    _bucket = srv.transfers.get(_k)
                    if _bucket is not None:
                        return self._json(_bucket)
            return self._json(srv.transfers.get(raw) or srv.transfers.get(quote(raw)) or {"max": 0, "total": 0})
        if path == "/api/health":
            cons = srv.fleet.consoles
            # THE ps5_* KEYS DESCRIBE THE PS5 - not whichever console happens to sort first.
            #
            # This read cons[0]. That is the PS5 only because reconcile_consoles orders it there,
            # and PS4-only is an ordinary way to run this app: with no PS5 configured, cons[0] IS
            # the PS4, so ps5_ip carried the PS4's address and ps5_online its readiness, and the
            # header painted the PS4 as a PS5 that was ready to install PS5 games.
            #
            # The test is `platform != "ps4"`, deliberately, NOT `== "ps5"`. A config written
            # before PS4 support existed names no platform at all, and consoles_from_cfg leaves
            # the field empty rather than inventing one; a strict "ps5" match would find nothing
            # and report an empty address for a PS5 that is sitting there working. The rule this
            # file follows throughout: a console that does not say what it is, is a PS5.
            _ps5 = next((c for c in cons
                         if str(c.get("platform") or "").lower() != "ps4"), None)
            b = srv.fleet.bridge(_ps5["id"]) if _ps5 else None
            # THE PS4 IS A SECOND CONSOLE, NOT A REPLACEMENT FOR THE FIRST. Everything below that
            # says ps5_* keeps meaning the PS5, because the whole UI and every peer reads those keys;
            # the PS4 gets its own pair beside them. A machine with no PS4 configured sends an empty
            # address and ps4_online false, which is what the settings panel shows as "not set up".
            _ps4 = next((c for c in cons
                         if str(c.get("platform") or "").lower() == "ps4"), None)
            _b4 = srv.fleet.bridge(_ps4["id"]) if _ps4 else None
            _ps4_on = False
            _ps4_rdy = False
            if _b4 is not None:
                # Same memo the PS5 probe uses, for the same reason: this endpoint is polled
                # every HEALTH_POLL_S and the answer is a round trip to a console.
                #
                # TWO FACTS OUT OF THE ONE ROUND TRIP. `ps4_online` means the PS4's shop is
                # answering - that is the question console_list and the presence dot are asking.
                # `ps4_engine_ready` is the PS4's own engine_ready flag, which is false when its
                # BGFT symbols did not resolve. Sending only the first is how the header pill read
                # "PS4 10.0.0.87 - ready to install" while Settings > Engine, two lines below it on
                # the same screen, correctly said the PS4 install service did not start, and every
                # install then failed. engine_available() was already fetching exactly this
                # /api/health body and throwing the flag away, so reading both costs nothing.
                _m4 = getattr(srv, "_ps4_probe", None)
                if _m4 and (time.monotonic() - _m4[0]) < HEALTH_MEMO_S:
                    _ps4_on = _m4[1]
                    _ps4_rdy = _m4[2] if len(_m4) > 2 else _m4[1]
                else:
                    _ps4_on = bool(_b4.up())
                    if _ps4_on:
                        try:
                            _h4 = _b4._shop("/api/health", timeout=4) or {}
                        except Exception:
                            _h4 = {}
                        # Still learn the platform from the answer, which is the one side effect
                        # engine_available() had beyond its return value.
                        _b4._note_platform(_h4)
                        _ps4_on = bool(_h4.get("on_console"))
                        # A PS4 ELF older than the engine_ready flag sends no such key. Falling
                        # back to on_console keeps that console reading exactly as it does today
                        # rather than painting a working PS4 as broken.
                        _ps4_rdy = bool(_h4.get("engine_ready", _h4.get("on_console")))
                    srv._ps4_probe = (time.monotonic(), _ps4_on, _ps4_rdy)
            # THE ELF INSTALLS THE DASHBOARD APP ITSELF now, the way the PS5 ELF installs its tile:
            # it carries the package, compares the installed version, and only acts when the console
            # has none or an older one. The PC used to do it here and must not any more - two
            # installers starting together would have the console refuse the second as busy and log a
            # failure for something that was working. Settings keeps its button for putting the app
            # back by hand, and GET /api/ps4/tile still reports what the console has.
            # PROBE MEMO. This endpoint is polled every 6 s by the console's UI, again by this
            # PC's own UI if it is open, and again by peers - and each call opened fresh TCP
            # connections to the console for ping() and ftp_ok(). On the console those connections
            # land on a single-threaded accept loop that is also serving the page the user is
            # scrolling, so the cost is paid in frames.
            #
            # Memoised HERE rather than inside ping()/ftp_ok(): those two are also called by the
            # install preflight, where a stale answer could matter. Health is a status readout
            # polled on a 6 s cycle; 5 s of staleness is invisible in it and cannot reach the
            # install lane.
            # ONE cheap question before any of the expensive ones. With the console switched off
            # every probe below pays its full timeout, and this endpoint is polled every 6s - the
            # app sat unresponsive for 10s at a time precisely when the user opened it to find out
            # why the console was not there. A closed TCP port settles it in milliseconds.
            reachable = b.up() if b else False
            # Only the FTP probe is memoised here now. A `dpi_reachable` used to be computed
            # beside it, stored, and never emitted - no dpi_* key has left this endpoint since
            # the third-party host was removed, so there was nothing for "old UIs" to read.
            _pm = getattr(srv, "_health_probe", None)
            if not reachable:
                ftp_on = False
            elif _pm and (time.monotonic() - _pm[0]) < HEALTH_MEMO_S:
                ftp_on = _pm[1]
            else:
                ftp_on = b.ftp_ok() if b else False
                srv._health_probe = (time.monotonic(), ftp_on)
            # WHAT "READY TO INSTALL" ACTUALLY MEANS NOW.
            # It used to mean "something is listening on :12800" - a third-party daemon we no
            # longer call. Our engine is spawned per install by Payload Manager and needs nothing
            # listening beforehand, so with etaHEN removed the app reported "not connected" while
            # installs worked perfectly. Ask OUR on-console server instead.
            # There is one engine. This used to read dpi.mode to decide whether to even ask the
            # console whether an install could start - and that key no longer exists, so leaving
            # the test in would have reported "third-party" and never probed at all.
            engine_ready = False
            if b is not None and reachable:
                # Cached: /api/health is polled continuously by the UI and this is a round trip
                # to the console. Measured 3.3 s per call before caching, which the user feels as a
                # sluggish app. HEALTH_MEMO_S is far shorter than the time it takes anyone to act
                # on what this says.
                _em = getattr(srv, "_engine_probe", None)
                if _em and (time.monotonic() - _em[0]) < HEALTH_MEMO_S:
                    engine_ready = _em[1]
                else:
                    try:
                        _h = b._shop("/api/health", timeout=4) or {}
                        engine_ready = bool(_h.get("engine_ready", _h.get("on_console")))
                    except Exception:
                        engine_ready = False
                    srv._engine_probe = (time.monotonic(), engine_ready)
            _b = _bridge_for(srv, self)
            # Only ask what is running when the console is actually there. This lookup costs two
            # 6-second timeouts against a dead address, which turned every health check into a
            # ~14s stall whenever the PS5 was off or resting — the app looked hung when in fact
            # the console simply was not home.
            # GATE ON THE BRIDGE ABOUT TO BE ASKED, not on the PS5. engine_ready and ftp_on
            # above describe `b` - the first non-PS4 console - while the question is put to `_b`,
            # the console this request is actually about. On a fleet with two PS5s, reading the
            # page on the second one while the first was switched off suppressed its running title
            # entirely, although that console was right there answering.
            #
            # When `_b` IS `b` - every single-console machine, and every PS5-plus-PS4 machine
            # where the viewer is the PS5 - the test is the original one and nothing moves. The
            # other branch is up(), a cached TCP connect, so it costs nothing when the console is
            # there and settles in milliseconds when it is not; and running_title() already
            # returns {} for a PS4 without touching the wire, so a PS4 is still asked nothing.
            _run_ok = ((engine_ready or ftp_on) if (_b is not None and _b is b)
                       else (_b.up() if _b is not None else False))
            _run = ((_b.running_title() if _run_ok else {}) if _b else {}) or {}
            # ...AND IF THAT CONSOLE HAS NO GAME, ASK THE OTHERS.
            #
            # `_b` is the console this REQUEST is about, which on a PC with no ?console= is the
            # first one configured - the PS5. A game running on the PS4 was therefore never asked
            # after, and the library never floated it to the top. The PS4 was answering the
            # question correctly the whole time; nobody was putting it.
            #
            # Preference is unchanged: the console the page is about still wins when it has a game,
            # so a single-console machine and a viewer sitting on a console both behave exactly as
            # before. This only fills a gap that used to read as "nothing is running anywhere".
            if not _run.get("titleId"):
                try:
                    for _ob in (srv.fleet.bridges.values() if getattr(srv, "fleet", None) else []):
                        if _ob is _b:
                            continue
                        if not _ob.up():          # cached TCP connect; a console that is off is free
                            continue
                        _o = _ob.running_title() or {}
                        if _o.get("titleId"):
                            _run = _o
                            break
                except Exception:
                    pass
            # Cached helper state, so adding these two keys costs health nothing - but only ask at
            # all when the console is actually there. helper_status() probes ShadowMount and FTP,
            # and against a switched-off console each of those waits out its own timeout.
            try:
                _helpers = (b.helper_status() if (b and reachable) else {}) or {}
            except Exception:
                _helpers = {}
            # The address this request came from, matched against the configured consoles. A plain
            # walk over at most a handful of entries; no probe, no timeout.
            try:
                _peer = (self.client_address or ("",))[0] or ""
            except Exception:
                _peer = ""
            _viewer_plat = viewer_platform_for(cons, _peer)
            return self._json({"ok": True, "version": VERSION,
                               # WHICH BUILD, not just which version - see build_id(). Two PCs
                               # reporting the same version and behaving differently is what this
                               # exists to make visible.
                               "build": build_id(),
                               # A newer build of this app already installed and waiting for a
                               # restart. Health is what every device polls, so this is how a phone
                               # or a console learns that the PC it is talking to has one ready.
                               "update_ready": app_update_state(),
                               # ...and the opposite: this copy came up without its own files. The
                               # app still works, so nothing else would ever mention it.
                               "degraded": _degraded,
                               "running_title": _run.get("titleId") or "",
                               "running_name": _run.get("titleName") or "",
                               "ps5_ip": (_ps5 or {}).get("ip") or srv.cfg.get("ps5_ip") or "",
                               # Reachability means OUR lane is usable: the ELF answers and
                               # Payload Manager can spawn the installer.
                               "ps5_online": engine_ready,
                               "engine": "pms-spawn",
                               "engine_ready": engine_ready,
                               "ftp_online": ftp_on,
                               "ftp_port": (getattr(b, "_ftp_port", None) if b else None)
                                           or (b.c.get("ftp_port") if b else None) or 2121,
                               "connected": (engine_ready or ftp_on),
                               "ps4_ip": (_ps4 or {}).get("ip") or srv.cfg.get("ps4_ip") or "",
                               "ps4_online": _ps4_on,
                               # THE PS4'S OWN VERDICT ON ITS ENGINE, beside the presence flag.
                               # ps4_online keeps meaning what it has always meant - the PS4's shop
                               # answers - because console_list below and the presence dot are about
                               # presence, and a console that is awake with a dead engine is still
                               # there. "Ready to install" is a different question and now reads a
                               # different key.
                               "ps4_engine_ready": _ps4_rdy,
                               "lan_ip": lan_ip(),
                               # The settings panel reads BOTH of these off health every few
                               # seconds. Neither was ever sent, so the poll kept overwriting the
                               # correct values that openSettings() had just fetched: the backups
                               # row flipped to a red "not running", and the library row fell back
                               # to a hardcoded path regardless of what was configured.
                               # None = unknown (ShadowMount is loopback-only, so the PC cannot
                               # see it). The UI must not paint unknown as "not running".
                               "shadowmount": _helpers.get("shadowmount"),
                               "shadowmount_port": int(_helpers.get("shadowmount_port") or 10101),
                               "library_paths": srv.cfg.get("library", {}).get("local_paths", []),
                               "companion_port": srv.cfg["companion"]["port"], "consoles": len(cons),
                               # THE CONSOLE'S OWN PORT, stated rather than guessed at.
                               #
                               # Settings prints "File transfer: <console ip>:<port>" and worked
                               # that port out as shop_port-or-companion_port - and health never
                               # sent a shop_port, so it landed on companion_port: THIS PC's
                               # listening port, printed against the console's address. It reads
                               # correct only because both default to 8710. Move the companion to
                               # another port - which its own settings offer - and the panel starts
                               # naming a port on the console that nothing there is listening on.
                               # The owner spotted exactly this shape of fault from a browser
                               # reaching for a port that did not belong to the machine it was
                               # aimed at.
                               "shop_port": int(srv.cfg.get("console", {}).get("shop_port", 8710)),
                               # EVERY CONSOLE, not just the one the ps5_* keys describe.
                               #
                               # `consoles` stays an integer. It has shipped as one, the page and
                               # peers read it as one, and changing a shipped key's type breaks
                               # them silently - so the list arrives as a NEW key beside it.
                               #
                               # Built entirely from probes that have ALREADY happened higher up:
                               # the PS5's readiness and the PS4's memoised answer. Nothing here
                               # opens a connection. Health is polled every 6 s by the page, again
                               # by this PC's own window and again by each peer, and on the console
                               # those round trips are paid for in the frames of whatever the user
                               # is scrolling. A console that is neither of those two - a third one
                               # someone added by hand - reports online: null, meaning "not asked",
                               # which the UI must paint as unknown rather than as a fault.
                               "console_list": [
                                   {"id": c.get("id"),
                                    "name": c.get("name") or c.get("ip") or "",
                                    "ip": c.get("ip") or "",
                                    "platform": str(c.get("platform") or "").lower() or "ps5",
                                    # ONE MEANING OF "ONLINE" IN THIS DOCUMENT, and it is the one
                                    # /api/consoles already uses: OUR shop answers. This read
                                    # `engine_ready or ftp_on`, and it disagreed with ps5_online
                                    # three keys above it within minutes of being written - the
                                    # PS5's payload stopped while its FTP, which belongs to the
                                    # jailbreak and outlives anything we load, kept answering. A
                                    # document that contradicts itself is worse than one that is
                                    # simply wrong: whichever half the UI believes, the other half
                                    # is on the screen beside it.
                                    "online": (_ps4_on
                                               if (_ps4 and c.get("id") == _ps4.get("id"))
                                               else (bool(engine_ready)
                                                     if (_ps5 and c.get("id") == _ps5.get("id"))
                                                     else None))}
                                   for c in cons],
                               # WHO IS READING THIS PAGE. The shop's UI is one file served by the
                               # PC and by both consoles, but the DATA always comes from here - so
                               # the page cannot tell on its own whether it is being read on a PS4,
                               # on a PS5 or on a PC, and it has to know: a PS4 must not be offered
                               # PS5 games, and the on-screen keyboard, the button hints and the
                               # focus ring differ per device.
                               #
                               # Answered from the requesting address, which is free and exact:
                               # the console asking is the console being read on. "" means a PC.
                               "viewer_platform": _viewer_plat,
                               "library_gen": srv.library.gen, "last_scan_ms": srv.library.last_scan_ms,
                               # Surfaced so the UI can warn instead of silently running on
                               # defaults while refusing to persist anything the user changes.
                               "config_unreadable": _CONFIG_UNREADABLE or "",
                               # Which config.json this process is actually reading. The exe
                               # reads the one BESIDE ITSELF, not companion/config.json, and
                               # that has cost whole test rounds; now the app says so.
                               "config_path": CONFIG_PATH,
                               "content_ack": bool(srv.cfg.get("content_ownership_ack"))})
        if path == "/api/engine/log":
            # Proxied, not re-implemented: the console writes this file with O_SYNC precisely so it
            # survives a crash, and it is the only record of an install that happened with this PC
            # switched off. Returned as JSON because everything else this API serves is JSON; the
            # console's own route stays plain text for anyone reading it with curl.
            b = _bridge_for(srv, self)
            if b is None:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            try:
                raw = b._shop_text("/api/engine/log", timeout=10)
                return self._json({"ok": True, "log": raw, "bytes": len(raw)})
            except Exception as e:
                # `b` is proven non-None four lines above. This field is read by curl,
                # tools/snapshot_api.py and tools/verify_console.py rather than by the page, and
                # all three were being told a PS4 fault was a PS5 one.
                return self._json({"ok": False,
                                   "error": "PKG MUTANT SHOP on the %s did not answer (%s)"
                                            % ("PS4" if b.is_ps4() else "PS5",
                                               str(e)[:80])}, 502)
        if path == "/api/engine/state":
            # WHY THIS EXISTS: the settings panel used to describe the install engine by reading
            # dpi_port out of /api/health and printing "ip:12800" next to an LED that meant "is a
            # third-party daemon listening". With our own engine that row was wrong in both halves
            # - the port belongs to software we removed, and the LED was off while installs worked.
            b = _bridge_for(srv, self)
            # ONE ENGINE, whatever an old config.json says. Ps5Bridge.install() always spawns
            # (install_spawn), so `mode` is reported as what actually runs. It used to be read
            # from dpi.mode with a default of "v2" - a key DEFAULT_CONFIG never carried - so a
            # fresh install reported ours:false, a "third-party install host" and never filled
            # busy/busy_for, while every install went through our engine regardless.
            # TWO CONSOLES, TWO ENGINES, AND THE PANEL MUST NAME THE RIGHT ONE.
            #
            # Every field below was written when a PS5 was the only thing this could describe, and
            # each one was wrong on a PS4 in a way that read as authoritative: the panel said
            # "Payload Manager - Port 8084 - answering" beside a green light, on a console that has
            # no Payload Manager, no port 8084 and no spawned installer. An invented green light is
            # worse than no light, because it sends someone hunting for the fault somewhere else.
            #
            # The PS4 installs through BGFT - the console's own download-and-install service, the
            # one the Debug Package Installer drives. There is no separate process to be ready and
            # nothing of ours listening on a port; what has to be true is that our payload resolved
            # the BGFT symbols, which is exactly what the PS4's own health calls engine_ready.
            _is4 = bool(b is not None and b.is_ps4())
            mode, ours = ("bgft" if _is4 else "spawn"), True
            pl_port = int((srv.cfg.get("dpi", {}) or {}).get("pldmgr_port", 8084))
            shop_port = int(srv.cfg.get("console", {}).get("shop_port", 8710))
            out = {
                "ok": True, "mode": mode, "ours": ours,
                "platform": (b.platform_id() if b else ""),
                "name": ("PKG MUTANT SHOP engine (PS4)" if _is4 else "PKG MUTANT SHOP engine"),
                "how": (("Installs are handed to the PS4's own download-and-install service, so a "
                         "finished install is a real one the console can launch.") if _is4 else
                        ("A fresh installer is started for every install and exits when the console "
                         "has accepted the package. Nothing stays running, so nothing can wedge.")),
                "shop_port": shop_port,
                # ZERO FOR A PS4, and the row hides it rather than printing ":0". The settings panel
                # prints this port whenever it has one, and a port number is a thing a person will
                # try to connect to before they doubt the panel.
                "pldmgr_port": (0 if _is4 else pl_port),
                "shop_ok": False, "shop_version": "", "ready": False,
                "busy": False, "busy_for": 0, "log_bytes": 0,
                "console_ip": (b.ip if b else srv.cfg.get("ps5_ip", "")),
            }
            if b is not None:
                try:
                    h = b._shop("/api/health", timeout=4) or {}
                    out["shop_ok"] = bool(h.get("ok"))
                    out["shop_version"] = str(h.get("version") or "")
                    # engine_ready on the console means "Payload Manager is answering", which is
                    # the one thing our lane actually needs before an install can start.
                    out["ready"] = bool(h.get("engine_ready", h.get("on_console")))
                    out["shadowmount"] = bool(h.get("shadowmount"))
                    out["ftp_port"] = int(h.get("ftp_port") or 0)
                except Exception as e:
                    # This route worked the platform out 30 lines above and then ignored it
                    # here. `detail` further down is already branched on _is4; this field was the
                    # one thing left in the answer still naming the wrong console.
                    out["error"] = ("PKG MUTANT SHOP on the %s did not answer (%s)"
                                    % ("PS4" if _is4 else "PS5", str(e)[:80]))
                if out["shop_ok"]:
                    # BOTH consoles answer this. The PS4 has no spawned installer, but it
                    # implements /api/engine/spawn-status against its own BGFT job and reports the
                    # same busy/busy_for - so "is an install running" is one question with one
                    # answer here. (It does not report log_bytes; 0 is then the truth, not a
                    # failure, and the panel shows the log button either way because
                    # /api/engine/log exists on both.)
                    st = b.spawn_status() or {}
                    out["busy"] = bool(st.get("busy"))
                    out["busy_for"] = int(st.get("busy_for") or 0)
                    out["log_bytes"] = int(st.get("log_bytes") or 0)
            # What the panel should SAY, decided once here rather than in three places in the UI.
            _dev = "PS4" if _is4 else "PS5"
            if not b:
                out["state"] = "no-console"
                out["detail"] = "No console is set up yet - add one above."
            elif not out["shop_ok"]:
                out["state"] = "shop-down"
                out["detail"] = (("PKG MUTANT SHOP is not answering on the PS4. Run the jailbreak, "
                                  "then open PKG MUTANT SHOP on the console.") if _is4 else
                                 ("PKG MUTANT SHOP is not answering on the PS5. Load it from "
                                  "Payload Manager, then reopen this panel."))
            elif not out["ready"]:
                out["state"] = ("engine-down" if _is4 else "no-pldmgr")
                out["detail"] = (("The PS4 install service did not start. Close PKG MUTANT SHOP on "
                                  "the console and open it again.") if _is4 else
                                 ("Payload Manager is not answering on :%d, and our engine needs "
                                  "it to start each install. Reload it on the PS5." % pl_port))
            elif out["busy"]:
                out["state"] = "busy"
                out["detail"] = "An install is being handed to the %s right now." % _dev
            else:
                out["state"] = "ready"
                out["detail"] = ("Ready - PS4 games can be installed straight away." if _is4
                                 else "Ready - installs can start straight away.")
            return self._json(out)
        if path == "/api/move/status":
            # Progress lives on the console (it is doing the copying), so this is a straight
            # forward. "not active, not done" is the honest answer when there is no console.
            b = _bridge_for(srv, self)
            if b is None:
                return self._json({"active": False, "done": False})
            try:
                return self._json(b._shop("/api/move/status", timeout=10) or
                                  {"active": False, "done": False})
            except Exception:
                return self._json({"active": False, "done": False})
        if path == "/api/devices":                                     # [B6]
            # The PS5 half comes FROM THE PS5. PS5_DEVICES is a hardcoded list of eleven ids with no
            # free space and no way to know which are plugged in, so the move picker used to offer
            # USB1-USB7 on a console that has none of them, each showing an undefined size. The
            # console answers this with real statvfs figures and a `detected` flag; the static list
            # is now only the fallback for when there is no console to ask.
            # EVERY CONSOLE, not just the first. This asked consoles[0] and called the answer
            # "ps5", so on a machine with a PS5 and a PS4 the PS4's drives were simply absent - the
            # app knew about the console and showed nothing about it anywhere a person looks for
            # devices. `ps5` still carries the first console's list, because the page and the peers
            # read that key and neither is being broken to add a second console.
            ps5 = PS5_DEVICES
            consoles = []
            # PROBED IN PARALLEL. This asked each console in turn with an 8 s budget of its own,
            # and platform_id() sat inside the same loop adding its own 3 s /api/health probe
            # whenever the platform is not written in config - which is every configuration from
            # before PS4 support existed, exactly the machine that has just had a PS4 attached. So
            # the drive picker and the storage bar took twice as long with two consoles as with
            # one, and a console that was busy rather than off made the whole request wait it out
            # before the other was even started. Nothing here depends on the previous answer.
            _dres, _dlock = {}, threading.Lock()

            def _probe_console(c, bridge):
                devs, ok = [], False
                try:
                    d = bridge._shop("/api/devices", timeout=8) or {}
                    if isinstance(d.get("ps5"), list) and d["ps5"]:
                        devs, ok = d["ps5"], True
                except Exception:
                    pass                                    # console asleep - say so, do not invent
                try:
                    plat = bridge.platform_id()
                except Exception:
                    # platform_id() swallows its own probe failures, so this catches only the
                    # impossible - and it answers the way that function does: a console that does
                    # not say what it is, is a PS5.
                    plat = str(c.get("platform") or "").lower() or "ps5"
                with _dlock:
                    _dres[c["id"]] = (devs, ok, plat)

            _dths = []
            for c in srv.fleet.consoles:
                b = srv.fleet.bridge(c["id"])
                if b is None:
                    continue
                _t = threading.Thread(target=_probe_console, args=(c, b), daemon=True)
                _t.start()
                _dths.append(_t)
            # One deadline for the whole fan-out, wide enough that a console which would have been
            # waited out serially (8 s of devices plus platform_id()'s 3 s health probe) is still
            # waited out here. Tightening it would make a slow-but-alive PS5 show as offline with
            # no drives, and the move and install pickers would lose their destinations.
            _ddl = time.time() + 14.0
            for _t in _dths:
                _t.join(timeout=max(0.05, _ddl - time.time()))

            # THE ASSEMBLY PASS STAYS SERIAL AND IN CONFIG ORDER. The legacy `ps5` key means "the
            # first console in config order that answered", not "whichever replied first on the
            # wire", so the flag below is resolved here and never set from inside a worker.
            _ps5_taken = False
            for c in srv.fleet.consoles:
                if srv.fleet.bridge(c["id"]) is None:
                    continue
                # A console whose probe overran the deadline is listed offline, exactly as a
                # timed-out serial probe listed it.
                devs, ok, plat = _dres.get(c["id"],
                                           ([], False,
                                            str(c.get("platform") or "").lower() or "ps5"))
                consoles.append({"id": c["id"], "name": c.get("name") or c["ip"], "ip": c["ip"],
                                 "platform": plat, "online": ok,
                                 "shop_port": srv.cfg.get("console", {}).get("shop_port", 8710),
                                 "devices": devs})
                # THE LEGACY `ps5` KEY MEANS THE PS5, not "whoever answered first". With the PS5
                # asleep and the PS4 awake it carried the PS4's eight destinations, and every
                # reader that falls back to it - consoleDevices() when the named console is absent,
                # ps5Destinations() with no console - then offered a drive that is physically
                # plugged into the other machine, with that machine's free space beside it.
                # A PS4-only setup leaves it as the static fallback rather than the PS4's list.
                if ok and plat != "ps4" and not _ps5_taken:
                    ps5 = devs
                    _ps5_taken = True
            # NO PS5 IN THIS FLEET, NO PS5 DEVICE LIST. Nothing above can fill `ps5` on a PS4-only
            # setup, so it was still PS5_DEVICES: eleven hardcoded destinations, none of them with a
            # free-space figure, for a console that is not in the house. The readers that fall back
            # to this key with no console id - driveLabelOf(), devicesStale(), moveGame() - then
            # offered them. The test is `!= "ps4"`, deliberately, so a console that names no
            # platform still counts as a PS5, which is the rule this file follows throughout; and it
            # reads the platform off the list built above, which is what the console itself said.
            # An EMPTY fleet keeps the static list, because a machine with no console configured yet
            # is the case that list was written for.
            if consoles and not any(c["platform"] != "ps4" for c in consoles):
                ps5 = []
            return self._json({"pc": enumerate_pc_drives(), "ps5": ps5, "consoles": consoles,
                               "library_paths": srv.cfg.get("library", {}).get("local_paths", [])})
        if path == "/api/psn-block":
            d = getattr(srv, "dns", None)
            out = d.status() if d else {"enabled": False, "listening_on": None,
                                        "error": "not started"}
            out["ok"] = True
            out["set_console_dns_to"] = out.get("listening_on") or _lan_ip()
            return self._json(out)
        if path.startswith("/api/verify/"):                            # [B7] structural completeness check
            raw = unquote(path[len("/api/verify/"):])
            rk = registry_key(srv.library.file_registry, raw)
            if not rk:
                return self._json({"error": "unknown key"}, 404)
            return self._json(dict(pkg_meta.pkg_completeness(srv.library.file_registry[rk]), key=rk))
        if path == "/api/ps5-log":
            with PS5_LOG_LOCK:
                return self._json({"log": list(PS5_LOG)})
        if path == "/api/config":
            return self._json({k: v for k, v in srv.cfg.items() if not k.startswith("_")})
        if path == "/api/ps4/tile":
            # What the settings panel shows for the PS4's dashboard app: does this copy of the app
            # even carry it, and is it on the console yet. Never installs - that is the POST.
            b = _ps4_bridge(srv)
            out = {"ok": True, "title_id": PS4_TILE_TID,
                   "available": os.path.isfile(PS4_TILE_PKG),
                   "console": bool(b), "installed": None}
            if b is not None:
                try:
                    out["installed"] = b.ps4_tile_installed()
                except Exception:
                    out["installed"] = None
            return self._json(out)
        if path == "/api/consoles":
            return self._json({"consoles": srv.fleet.status()})
        if path == "/api/library":
            return self._json(build_federated_library(srv))
        if path == "/api/federation":                                  # [B9] advertise our library to peers
            return self._json(federation_self(srv))
        if path == "/api/network":
            # Everything on the network that is part of this setup: this PC, the peers we can
            # see, and the console. This is what the UI shows and what a phone connects to.
            reg = getattr(srv, "peers", None)
            if reg is not None:
                reg.scan_async()
            me = device_identity(srv.cfg)
            b = _bridge_for(srv, self)
            return self._json({
                "this_pc": {"id": me.get("id"), "name": me.get("name"),
                            "lan_ip": lan_ip(), "port": srv.cfg["companion"]["port"],
                            "url": "http://%s:%d" % (lan_ip(), srv.cfg["companion"]["port"]),
                            "os": platform.system(), "version": VERSION,
                            "counts": _library_counts(srv), "local": True},
                "peers": [{"id": p.get("id"), "name": p.get("name"), "lan_ip": p.get("lan_ip"),
                           "url": p.get("url"), "os": p.get("os"), "version": p.get("version"),
                           "counts": p.get("counts"), "count": p.get("count", 0),
                           "online": bool(p.get("online")), "last_seen": p.get("last_seen")}
                          for p in (reg.known() if reg is not None else [])],
                # OUR SHOP ANSWERING is the liveness word every other route in this file uses.
                # This asked Payload Manager on :8084 - a PS5-only tool that no PS4 has ever run -
                # so whenever the bridge resolved to the PS4 (the page open on the PS4 itself) this
                # key said offline for a console that was awake and installing, and the question
                # cost a connect to a port that is not there. up() is the same cached TCP probe
                # /api/health uses, on the console's own shop port, and a PS5 whose Payload Manager
                # is not loaded yet is a live shop too - so this is the better answer for both.
                "console": {"ip": b.ip, "name": b.name, "online": b.up()} if b else None,
                # EVERY CONSOLE, BY NAME AND ADDRESS. Settings' "Open it on the TV" row reads
                # d.consoles, and this route never sent one - so it fell back to a single address
                # and the PS4's, the one thing you need to type into the PS4's own browser, was
                # shown nowhere in the whole panel. (The comment above that row says it was fixed
                # for exactly this case; it was written against a key nothing sent.)
                #
                # No probe here on purpose: this route is polled by the panel and the address is a
                # FACT ABOUT THE CONFIGURATION, not about whether the console is awake. The LEDs
                # beside it come from health, which does probe.
                "consoles": [{"id": c.get("id"), "name": c.get("name") or c.get("ip"),
                              "ip": c.get("ip"),
                              "platform": str(c.get("platform") or "").lower() or "ps5",
                              "shop_port": int(srv.cfg.get("console", {}).get("shop_port", 8710))}
                             for c in srv.fleet.consoles],
                "scanning": bool(reg.scanning) if reg is not None else False})
        if path == "/api/network/scan":
            if not self._origin_ok():
                return self._refuse_cross_site()
            reg = getattr(srv, "peers", None)
            if reg is None:
                return self._json({"ok": False, "error": "peer registry unavailable"}, 500)
            # scan() spawns 254 threads and blocks this request until every one finishes, so a
            # press of "Scan network" held the whole single-request path open for the full sweep.
            # scan_async() is the same sweep with a 60 s minimum interval (already used by the two
            # other callers). Kick it off, then give it a SHORT bounded grace period so the button
            # still returns something useful on a cold first press instead of an empty list -
            # rather than either blocking for the full sweep or lying about finding nothing.
            before = len(reg.known())
            reg.scan_async()
            deadline = time.monotonic() + 2.5
            while time.monotonic() < deadline and len(reg.known()) == before:
                time.sleep(0.1)
            found = reg.known()
            return self._json({"ok": True, "found": len(found),
                               "peers": [{"id": p.get("id"), "name": p.get("name"),
                                          "url": p.get("url"), "counts": p.get("counts")}
                                         for p in found]})
        if path == "/api/federation/peers":
            # The registry's known peers, in the same shape /api/library's `peers` uses. This
            # read a `_fed_peers` attribute that nothing ever set, so it answered [] for ever.
            reg = getattr(srv, "peers", None)
            _known = reg.known() if reg is not None else []
            # `enabled` USED TO REPORT A CONFIG FLAG THAT GATES NOTHING, and it reported it as
            # False on a machine that was fully paired - the owner read that and concluded the two
            # PCs were not talking, while every payload on one was already reachable from the other.
            # Auto-discovery is unconditional (the registry scans whether or not the flag is set)
            # and `federation.peers` is only a list of HAND-WRITTEN extras, so the flag has meant
            # nothing since discovery landed. Report what is true instead: are we federated?
            _cfg = srv.cfg.get("federation", {})
            return self._json({"peers": [peer_summary(p) for p in _known],
                               "enabled": bool(_known) or bool(_cfg.get("peers")),
                               "discovery": True,
                               "configured": len(_cfg.get("peers") or [])})
        if path == "/api/storage":
            b = _bridge_for(srv, self)
            devs = None
            if b is not None:
                try:
                    devs = (b._shop("/api/devices", timeout=8) or {}).get("ps5")
                except Exception:
                    devs = None                     # console asleep
            # THE PROBE ABOVE IS THE LIVENESS TEST, not the app cache. It is a real round-trip
            # behind the cached TCP gate, so it is free when the console is off; and a console that
            # is up always answers it with all eleven destinations. devs is None therefore means
            # exactly "the PS5 did not answer", and its drives drop off the bar until it does.
            _out = build_storage(srv, b.console_apps() if b else None, devs,
                                 console_online=devs is not None)
            # EVERY CONSOLE'S DRIVES, NOT JUST THE RESOLVED ONE. This route described exactly one
            # console - _bridge_for's answer - so on the PC, which manages both, the PS4's internal
            # HDD and its sticks were invisible: the storage bar showed the PS5 and nothing else,
            # and there was no way to see how full the PS4 was without walking to it. The resolved
            # console's tiles are the ones built above, in the same order with the same ids, so
            # every existing reader sees what it saw before; the other consoles' tiles are appended
            # after them and before the PC tiles, which build_storage deliberately keeps last.
            #
            # Every console tile also names its console, because the labels collide: "USB 0" is
            # byte-identical on a PS5 and a PS4, and two unlabelled tiles in one row cannot be told
            # apart. The name is folded into the label only when the fleet HAS a second console -
            # decided from the configuration, not from who happens to be awake, so the row does not
            # rename itself every time the PS4 goes to sleep. A single-console bar is untouched.
            _bid = (b.c.get("id") if b is not None else None)
            _multi = len(srv.fleet.consoles) > 1
            # HOW LONG THE OTHER CONSOLE'S DRIVE LIST STAYS GOOD. The bar is polled every 15 s from
            # a PC and every 60 s from a console, and this memo is deliberately LONGER than the
            # shorter of those for the reason HEALTH_MEMO_S is written out at the top of this file:
            # a memo shorter than the poll it protects is thrown away one tick before the poll that
            # would have used it, so it costs the round trip and saves nothing. Free space does not
            # move in twenty seconds, and the console's accept loop is the same one serving the
            # page the owner is scrolling.
            _dmemo = getattr(srv, "_storage_devs", None)
            if _dmemo is None:
                _dmemo = srv._storage_devs = {}
            _extra = []
            for _c in srv.fleet.consoles:
                if not _bid or _c.get("id") == _bid:
                    continue
                _ob = srv.fleet.bridge(_c["id"])
                # The cached TCP gate first: a console that is off or resting settles in
                # milliseconds here instead of waiting out an HTTP timeout, so an asleep second
                # console costs this route nothing at all.
                if _ob is None or not _ob.up():
                    continue
                _hit = _dmemo.get(_c["id"])
                if _hit and (time.monotonic() - _hit[0]) < 20.0:
                    _od = _hit[1]
                else:
                    try:
                        _od = (_ob._shop("/api/devices", timeout=8) or {}).get("ps5")
                    except Exception:
                        _od = None              # went away between the gate and the question
                    _dmemo[_c["id"]] = (time.monotonic(), _od)
                if _od is None:
                    continue
                _oname = _c.get("name") or _c.get("ip") or _c.get("id")
                for _t in build_storage(srv, _ob.console_apps(), _od,
                                        console_online=True).get("drives", []):
                    if _t.get("kind") != "console":
                        continue                # this PC and the peers are built once, above
                    _t["console_id"] = _c.get("id")
                    _t["console_name"] = _oname
                    _t["label"] = "%s %s" % (_oname, _t.get("label") or _t.get("id"))
                    _extra.append(_t)
            if _bid:
                _bname = (b.name if b is not None else "")
                for _t in _out.get("drives", []):
                    if _t.get("kind") != "console":
                        continue
                    _t["console_id"] = _bid
                    _t["console_name"] = _bname
                    if _multi:
                        _t["label"] = "%s %s" % (_bname, _t.get("label") or _t.get("id"))
            if _extra:
                _out["drives"] = ([_d for _d in _out["drives"] if _d.get("kind") != "pc"] + _extra +
                                  [_d for _d in _out["drives"] if _d.get("kind") == "pc"])
            return self._json(_out)
        if path == "/api/console/apps":
            b = _bridge_for(srv, self)
            apps = b.console_apps() if b else None
            # `reachable` is a LIVE question and gets a live answer: console_apps() serves its
            # last good list for the life of the process (right for browsing, wrong as a
            # liveness word - it stayed true for hours after the PS5 went off). The list is
            # still the cached one.
            return self._json({"reachable": bool(b and b.up()), "apps": apps or []})
        if path == "/api/discover":
            if not self._origin_ok():
                return self._refuse_cross_site()
            return self._json({"found": discover_ps5(srv.cfg)})
        if path == "/api/installed":
            # WHAT IS INSTALLED IS A FACT ABOUT THE FLEET, and installed.json is the only record of
            # it when every console is off. This asked ONE console and then pruned that file down to
            # what that one console had - so on a machine with a PS5 and a PS4, every load that
            # happened to resolve to the PS4 silently deleted the memory of every PS5 install, and
            # the other way round. Invisible while a console is reachable, because the library's
            # on_console flag is a union and carries the badges; permanent, and visible the moment
            # the app is restarted with both consoles asleep - games lose their Installed badge and
            # their updates and DLC reappear as orphan cards.
            #
            # A named console still gets ITS OWN answer, which is what the panel asks for. The
            # PRUNE, though, only ever happens against the union of every console that answered -
            # and only when they all did, because a console that did not answer has not disowned
            # anything. `console=all` asks everyone and returns the union.
            cid = (q.get("console") or [None])[0]
            want_all = str(cid or "").lower() == "all"
            per, missed = {}, 0
            for _cid in srv.fleet.ids():
                _b = srv.fleet.bridge(_cid)
                got = _b.installed_titles() if _b is not None else None
                if got is None:
                    missed += 1
                else:
                    per[_cid] = set(got)
            union = set()
            for v in per.values():
                union |= v

            if per and not missed:
                # Every console spoke: the union is authoritative, so a title in the file that no
                # console has is genuinely a ghost.
                prune_local_installed(sorted(union))

            if want_all:
                asked = sorted(union) if per else None
            elif cid and cid in per:
                asked = sorted(per[cid])
            elif cid:
                asked = None                              # that console did not answer
            else:
                _b = _bridge_for(srv, self)
                # A bridge keeps its console dict in .c - there is no .id property, and a getattr
                # for one would quietly be None and send every caller to the union.
                _id = (_b.c.get("id") if _b is not None else None)
                asked = sorted(per[_id]) if (_id in per) else (sorted(union) if per else None)

            merged = asked if asked is not None else sorted(local_installed())
            return self._json({"installed": merged,
                               "source": "console" if asked is not None else "local",
                               "console_reachable": asked is not None,
                               # Which consoles actually answered, so a caller can tell "nothing is
                               # installed there" from "it did not say".
                               "answered": sorted(per), "silent": missed})
        if path == "/api/sources":
            # RANK ONCE. best() re-enters rank() -> probe_all(), which rebuilds one thread per
            # source and re-sorts microseconds after the first sweep has stored every probe. The
            # test below is best()'s own rule - ok, and has_file in (True, None) - applied to the
            # list already in hand, and `ranked` is already sorted by the same key, so the name
            # chosen is the same one. It also closes the single case where the duplication cost
            # real work: probe_all joins at 4 s while probe_source is given 2.5, so a source that
            # overran the join was missing from the first sweep AND still uncached, and the
            # re-entry then fired a SECOND concurrent probe at the source that was already too
            # slow. .get("ok") rather than ["ok"], because a KeyError here would take out the
            # source bar and the Settings sources list together.
            ranked = srv.engine.rank()
            _best = next((p for p in ranked
                          if p.get("ok") and p.get("has_file") in (True, None)), None)
            _hb = _bridge_for(srv, self)          # one lookup, not two
            doc = {"local_paths": srv.cfg["library"]["local_paths"],
                   "lan_url": "http://%s:%d/library/" % (lan_ip(), srv.cfg["companion"]["port"]),
                   "sources_file": srv.cfg["library"].get("sources_file"),
                   "sources": ranked, "best": (_best or {}).get("name"),
                   "helpers": (_hb.helper_status() if _hb else {})}
            return self._json(_with_peer_sources(srv, doc))
        if path.startswith("/api/hash/"):
            raw = unquote(path[len("/api/hash/"):])
            rk = registry_key(srv.library.file_registry, raw)
            if not rk:
                return self._json({"error": "unknown key"}, 404)
            p = srv.library.file_registry[rk]
            try:
                size = os.path.getsize(p)
            except OSError as e:
                return self._json({"error": str(e)}, 500)
            known = cached_sha(p, srv.hashes)
            if known:
                return self._json({"key": rk, "sha256": known, "size": size, "cached": True})
            # The hash is computed on THIS request thread, reading the whole file. An 85 GB
            # package held a server thread for many minutes while the browser gave up, and
            # every re-click of the button started another full read of the same file.
            if size > _HASH_MAX_BYTES:
                return self._json({"ok": False, "key": rk, "size": size, "error": "too_large",
                                   "message": "This file is over 8 GB, so it was not hashed - "
                                              "reading all of it would tie up the PC for a "
                                              "long time."}, 413)
            with _HASH_LOCK:
                if p in _HASHING:
                    return self._json({"ok": False, "key": rk, "error": "busy",
                                       "message": "This file is already being hashed - "
                                                  "wait for that to finish."}, 409)
                _HASHING.add(p)
            try:
                return self._json({"key": rk, "sha256": sha256_of(p, srv.hashes), "size": size})
            except OSError as e:
                return self._json({"error": str(e)}, 500)
            finally:
                with _HASH_LOCK:
                    _HASHING.discard(p)
        if path.startswith("/api/manifest/"):
            return self._manifest(unquote(path[len("/api/manifest/"):]))
        if path == "/api/cheats/paths":
            b = _bridge_for(srv, self)
            if not b:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            info = b.cheat_paths() or {}
            # PASS THROUGH A REFUSAL THE CONSOLE ACTUALLY MADE - and only that.
            #
            # This used to refuse on `b.is_ps4()` as well, which was right when it was written: the
            # PS4 payload answered every cheat route with "not available on the PS4 yet", and
            # bolting an upload URL and five drop folders onto that refusal would have sent an
            # owner hunting for a missing cheat file for a feature that did not exist. It now has
            # the engine, files dropped cheats itself, and answers this route with its real drop
            # folder and library paths - so deciding here what that console can do overrides the
            # console's own answer with a stale belief. `unsupported` is the console speaking, and
            # that is still honoured.
            if info.get("unsupported"):
                return self._json(info or {"ok": False, "unsupported": True, "platform": "ps4",
                                           "error": "mods_not_on_ps4",
                                           "message": "Mods and cheats are not available on the "
                                                      "PS4 yet"})
            # How to actually get files onto the console. FTP is the fallback now, not the
            # route - so name our own file API first and only mention FTP when one is running.
            port = int(info.get("shop_port") or b.cfg.get("console", {}).get("shop_port", 8710))
            info["upload"] = "http://%s:%d/api/fs/write?path=<destination>" % (b.ip, port)
            live_ftp = b._ftp_port if b.ftp_ok(timeout=1.0) else None
            info["ftp"] = ("ftp://%s:%d" % (b.ip, live_ftp)) if live_ftp else ""
            info["ftp_port"] = live_ftp or 0
            return self._json(info)
        if path == "/api/engine/agent":
            # THE IN-GAME HELPER, READ FROM A PC. Only the console served this, so from the desktop
            # app the control reported nothing and could not be changed - see the POST twin below
            # for the half that writes. A plain relay: the console owns the answer, this just puts
            # the question to the right machine.
            b = _bridge_for(srv, self)
            if not b:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            return self._json(b.engine_agent() or
                              {"ok": False, "error": "console did not answer"})
        if path == "/api/cheats/rescan":
            if not self._origin_ok():
                return self._refuse_cross_site()
            b = _bridge_for(srv, self)
            if not b:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            return self._json(b.cheat_rescan() or {"ok": False})
        if path == "/api/rest/prepare":
            # A plain GET, so a hidden <img src> on a hostile page could stop every homebrew
            # payload on the console. The Origin/Referer guard is what makes that impossible; an
            # <img> from a public page carries that page's Referer - and when it does not (a
            # no-referrer page), its Sec-Fetch-Dest says "image", which the guard also refuses.
            if not self._origin_ok():
                return self._refuse_cross_site()
            # Stopping the console's payloads is most useful from HERE — you are about to walk
            # away and rest the console, so driving it from the PC or a phone beats having to
            # open the app on the PS5 first. Relay it; the console does the actual work.
            b = _bridge_for(srv, self, want="ps5")
            if not b:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            q = urlparse(self.path).query
            # OUR OWN QUEUE FIRST. The console's busy test covers its own download/install job;
            # it cannot see a spawned base-game install that is mid-transfer from this PC, nor a
            # backup this PC is pushing over /api/fs/write. Stopping the payloads under either
            # errors the queue with "console unreachable" while BGFT carries on, or leaves a
            # half-written .part in a watch folder. Refuse while any of our tasks is live.
            # For the install lane, "promoting" means every byte has crossed and BGFT (a system
            # job) owns the install: nothing is leaving this PC, so stopping the payloads cannot
            # corrupt it - yet it used to refuse this button for up to 35 min with a sentence
            # that was untrue in that state. The mount/peer lanes keep the full set: their
            # "promoting" waits on ShadowMount and a pull may still be in flight.
            _pc_busy = {"claimed", "verifying", "submitting", "transferring"}
            _live = [x for x in srv.queue.snapshot()
                     if x.get("state") in (_pc_busy if x.get("lane") == "install" else RUNNING)]
            if _live and "force=1" not in q:
                _verb = ("being mounted on" if _live[0].get("state") == "promoting"
                         else "being sent to")
                return self._json({"ok": False, "error": "busy",
                                   "message": "%s is still %s the PS5 - let it finish "
                                              "before stopping the console's payloads."
                                              % (_live[0].get("name") or "An install", _verb)}, 409)
            # Establish FIRST that the console is actually there. The call below quits the app on
            # the console as its last step, so a dropped connection is the normal ending — but only
            # if it was reachable to begin with. Without this check an offline PS5 would report
            # "stopped, safe to rest", which is the one wrong answer this button must never give.
            try:
                b._shop("/api/health", timeout=6)
            except Exception as e:
                # The repr goes to pms.log; the person gets a sentence they can act on rather
                # than a Python exception in a toast.
                print("[rest] prepare refused - the console did not answer /api/health: %r" % (e,))
                return self._json({"ok": False, "error": "console_unreachable",
                                   "message": "PKG MUTANT SHOP on the PS5 is not answering - load "
                                              "it from Payload Manager, then try again."}, 502)
            try:
                return self._json(b._shop("/api/rest/prepare" + ("?" + q if q else ""), timeout=45))
            except (ConnectionResetError, http.client.RemoteDisconnected,
                    http.client.IncompleteRead, BrokenPipeError) as e:
                # The console answers and then _exit()s (server.c: send_json, close, _exit), so
                # the only way to lose the answer is the socket closing under the read - which is
                # the app quitting as its last step. That, and only that, is the good ending.
                return self._json({"ok": True, "self_quit": True, "detached": True,
                                   "note": "the console shut the app down while answering (%s)" % e})
            except Exception as e:
                # A TIMEOUT IS NOT A SUCCESS. Forty-five seconds with no answer means the console
                # was still killing payloads (or never finished), and this branch used to report
                # ok:true "self_quit" for it - "safe to rest" is the one wrong answer this button
                # must never give.
                if isinstance(e, urllib.error.URLError) and isinstance(
                        getattr(e, "reason", None),
                        (ConnectionResetError, http.client.RemoteDisconnected, BrokenPipeError)):
                    return self._json({"ok": True, "self_quit": True, "detached": True,
                                       "note": "the console shut the app down while answering (%s)" % e})
                print("[rest] prepare relay did not complete: %r" % (e,))
                return self._json({"ok": False, "error": "no_answer",
                                   "message": "The PS5 did not finish stopping its payloads - "
                                              "check the console before resting it."}, 502)
        if path == "/api/payloads/autostart":
            # Helper auto-start is a console setting, so this is only ever a relay. Without it the
            # button 404s here and looks broken, even though it works on the PS5 itself — and the
            # whole point of the toggle is being able to flip it from a PC or a phone.
            # Reading the setting is harmless; WRITING it (any query: on=, delay=) is a console
            # change that must not be reachable from a hostile page's <img>.
            if urlparse(self.path).query and not self._origin_ok():
                return self._refuse_cross_site()
            b = _bridge_for(srv, self, want="ps5")
            if not b:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            # Pass the whole query through rather than one named parameter: the console owns this
            # setting and now takes `delay` as well as `on`, and a relay that only knew about `on`
            # would silently drop it.
            q = urlparse(self.path).query
            try:
                # Tiny call. A console that cannot answer in 5s is not there, and waiting longer
                # only leaves the button visible-but-doomed for that much more of the panel's life.
                return self._json(b._shop("/api/payloads/autostart" + ("?" + q if q else ""),
                                          timeout=5))
            except Exception as e:
                return self._json({"ok": False, "error": "console did not answer: %s" % e}, 502)
        if path.startswith("/api/mods/"):
            tid = unquote(path[len("/api/mods/"):]).strip("/")
            # ?state=1 is the quiet refresh the mods panel fires after every toggle. It reads
            # r.mods and nothing else, so composing the patch document for it cost a console
            # round trip per click - and on a title with no installed version that round trip
            # makes the console read the whole app.db and walk its b-tree. Anything else, and
            # anything unparseable, still means the full document the panel's first paint needs.
            state_only = (parse_qs(u.query).get("state") or [""])[0] == "1"
            b = _bridge_for(srv, self)
            if not b:
                return self._json({"ok": False, "error": "no console configured"}, 400)

            # What version is actually installed? Look this up FIRST so our engine can pick the
            # exactly-matching cheat file instead of settling for whatever version it finds.
            installed_ver = ""
            for a in (b.console_apps() or []):
                if a.get("title_id") == tid:
                    installed_ver = a.get("app_ver") or ""
                    break

            # ?version= LETS THE PANEL ASK FOR A DIFFERENT CHEAT FILE THAN THE INSTALLED VERSION'S.
            #
            # A title often has several cheat files, one per game version, and they are NOT equal:
            # Dark Souls II ships 1 mod for 01.00 and 7 for 01.02. The engine correctly prefers the
            # exact match, so an owner on 01.00 saw a single mod and no way to reach the other six -
            # "mods are missing" from where they sit. This is the deliberate way to pick another
            # one. It is not a free-for-all: the engine's expect-gate still refuses any write whose
            # target does not already hold the bytes that file documents, so a file written for
            # another build fails closed instead of corrupting the game.
            want_ver = (parse_qs(u.query).get("version") or [""])[0].strip()
            pick_ver = want_ver or installed_ver

            # OUR engine answers first, for ANY title. When the game is running we also get its
            # pid/base, so mods report live on/off state and can be toggled; when it is not we
            # still list them (read-only) instead of pretending the title has no cheats.
            # CheatRunner is a last resort, never a requirement.
            mine = b.mutant_running(pick_ver)
            # "THE GAME IS RUNNING" IS NOT "A CHEAT CAN BE WRITTEN INTO IT".
            #
            # On a PS5 those are one question: the payload reaches the game's memory itself, so a
            # running game with a cheat file is a game that can be toggled. On a PS4 they come
            # apart, and this is the ordinary case rather than an edge one - the engine lives in a
            # small helper GoldHEN loads INTO the game, so a game started before the helper was
            # listed is running, has a cheat file, and cannot be written to at all. The console
            # says so plainly: running:true, can_cheat:false, pid 0.
            #
            # Reading `live` from title + file alone offered every tile as pressable, sent the
            # write to pid 0, and reported a failure that reads as a bad cheat file. A console that
            # does not report can_cheat (an older PS5 build) is unchanged: absent means "do not
            # know", and the old answer stands.
            # Is the game that is running the one this request is about? Hoisted because five
            # fields below need it and three of them used to skip it.
            same_game = bool(mine and mine.get("title_id") == tid)
            live = bool(same_game and mine.get("cheat_file"))
            if live and "can_cheat" in (mine or {}):
                live = bool(mine.get("can_cheat"))
            # When the panel asked for a specific version, that choice wins over whatever file the
            # running-game lookup settled on - otherwise picking 01.02 would silently keep showing
            # the 01.00 file the engine prefers.
            src = mine if live else {}
            if want_ver or not src.get("cheat_file"):
                found = b.mutant_find(tid, pick_ver) or {}
                if found.get("cheat_file"):
                    if want_ver:
                        src = dict(src or {})
                        src["cheat_file"] = found["cheat_file"]
                        src["match"] = found.get("match", "")
                        src["exact"] = found.get("exact")
                    else:
                        src = found
            if src.get("cheat_file"):
                pid = mine.get("pid") if live else None
                base = mine.get("base") if live else None
                doc = b.mutant_mods(src["cheat_file"], pid, base) or {}
                if doc.get("ok"):
                    fv = doc.get("version") or cheat_file_version(src["cheat_file"])
                    # An UNKNOWN installed version is not the same as a mismatch. PS5 titles
                    # often expose no APP_VER, and treating that as incompatible hid every
                    # cheat for them. Only flag a mismatch when we can actually prove one.
                    # NOT `src["exact"]`. That comes from mutant_find(tid, pick_ver) and means
                    # "exact against what was ASKED FOR" - so deliberately picking a version other
                    # than the installed one reported compatible:true, and the panel painted the row
                    # with the ok colour and "matched", skipping the mismatch note entirely (it lives
                    # in the if(!okv) arm). Proven live: PPSA01342 with version=01.00 installed on
                    # 01.02 came back compatible. The question this field answers is whether the file
                    # suits the game ON THE CONSOLE, and only installed_ver can answer it.
                    compatible = (not installed_ver) or (fv == installed_ver)
                    reason = src.get("match", "")
                    if not installed_ver and reason == "other version":
                        reason = "installed version unknown"
                    return self._json({
                        "ok": True, "reachable": True, "engine": "mutant", "title_id": tid,
                        "installed_version": installed_ver, "file_version": fv,
                        # Which version the panel is CURRENTLY showing, so a version picker can
                        # mark the active one without re-deriving it from the file name.
                        "selected_version": want_ver or fv,
                        "file": src["cheat_file"].rsplit("/", 1)[-1],
                        "path": src["cheat_file"],
                        "format": doc.get("format") or _cheat_format(src["cheat_file"]),
                        "compatible": compatible, "reason": reason,
                        # `running` is what the panel gates its tiles on, so it carries the
                        # can_cheat meaning above. The console's own view is kept beside it under
                        # its own names, so the UI can say WHY a running game cannot be toggled
                        # instead of falling back to "not running", which is not true and sends
                        # the owner to relaunch a game that is already on screen.
                        "running": live, "pid": pid, "base": base,
                        # SAME TITLE OR NOTHING. `running` and `game_running` both test
                        # mine["title_id"] == tid, but helper, can_cheat and cheat_blocked below used
                        # to read `mine` unconditionally - and `mine` is mutant_running(), an
                        # unfiltered read of whatever is running on that console. So with game A
                        # running and the panel open on title B, those three described A: the panel
                        # would report a helper present and a reason that belonged to another game.
                        "game_running": same_game,
                        "helper": (mine or {}).get("helper") if same_game else None,
                        "can_cheat": (mine or {}).get("can_cheat") if same_game else None,
                        # WHY a running game cannot be written to, in the console's own words.
                        # "no_engine" means this console has no way to reach a running process at
                        # all - not that something needs relaunching. Telling somebody to restart
                        # a game that is on screen in front of them is the kind of wrong answer
                        # that sends people hunting through cheat files for an hour.
                        "cheat_blocked": ((mine or {}).get("why") or "") if same_game else "",
                        # IS THE HELPER ALREADY SET UP FOR THIS TITLE? Only a PS4 has one, and this is
                        # only asked when the panel might otherwise offer a button - i.e. when the
                        # helper is not already in the running game - so the common path costs nothing.
                        **(_helper_arm_state(b, tid) if (not live and b.is_ps4()) else {}),
                        # A MASTER CODE CHANGES WHAT "OFF" MEANS. When the file carries one the
                        # engine installs it before the first mod, and for the json form there are no
                        # documented original bytes to put back - so turning every cheat off leaves
                        # it in the running game and only closing the game clears it. The panel says
                        # so once. Absent from an older payload, and absent means "no note".
                        "master": bool(doc.get("master")),
                        "master_removable": bool(doc.get("master_removable")),
                        "platform": (mine or {}).get("platform") or ("ps4" if b.is_ps4() else "ps5"),
                        "mods": [{"index": m.get("index"), "name": m.get("name"),
                                  "entries": m.get("entries"),
                                  "state": m.get("state") or "unknown",
                                  "on": bool(m.get("on")),
                                  "conflict": (m.get("state") == "partial"),
                                  # Toggling writes into the game's memory, so it needs the game
                                  # running. Listing does not.
                                  "can_toggle": live, "conflicts_with": []}
                                 for m in (doc.get("mods") or [])],
                        # Game patches live in their own XML library, so they are fetched
                        # separately — this was a hardcoded [] and kept 376 files invisible.
                        "patches": ([] if state_only else b.mutant_patches(tid, installed_ver)),
                        "candidates": [],
                        "versions": doc.get("versions") or []})
            cr = b.cheats
            if not cr.alive():
                # Our own engine already had its turn above, so reaching here means we simply
                # have no cheat file for this title -- not that anything is offline.
                # A title with no CHEATS can still have game PATCHES (Dark Souls Remastered is
                # exactly that), so ask for those before declaring there is nothing here.
                pat = b.mutant_patches(tid, installed_ver)
                return self._json({"ok": bool(pat), "reachable": True, "engine": "mutant",
                                   "error": "" if pat else "no_local_cheat_found",
                                   "title_id": tid, "installed_version": installed_ver,
                                   "mods": [], "patches": pat, "candidates": []})
            found = cr.find(tid)
            cands = []
            for c in (found.get("candidates") or []):
                fv = cheat_file_version(c.get("filename", ""))
                cands.append(dict(c, file_version=fv,
                                  compatible=(not installed_ver or not fv or fv == installed_ver)))
            best = found.get("path") or ""
            best_ver = cheat_file_version(found.get("filename", ""))
            doc = cr.doc(tid) if found.get("ok") else {}
            st = cr.state(tid) if found.get("ok") else {}
            # CheatRunner reports each mod as state/label ("on" | "off" | "conflict") plus
            # can-toggle flags and a conflict list. Reading .on/.enabled (which do not exist)
            # made every mod look disabled even after it had been turned on.
            by_index = {}
            for row in (st.get("mods") or []):
                if isinstance(row, dict) and row.get("index") is not None:
                    by_index[int(row["index"])] = row
            return self._json({"ok": bool(found.get("ok")), "reachable": True, "title_id": tid,
                               "installed_version": installed_ver,
                               "file": found.get("filename", ""), "path": best,
                               "file_version": best_ver, "format": found.get("format", ""),
                               "compatible": (not installed_ver or not best_ver or best_ver == installed_ver),
                               "reason": found.get("selectedReason", ""),
                               "candidates": cands,
                               "mods": [_merge_mod(i, m, by_index.get(i, {}))
                                        for i, m in enumerate(doc.get("mods") or [])],
                               "patches": doc.get("patches") or [],
                               "error": found.get("error", "")})
        if path == "/api/cheats/library":            # what we ship vs what the console has
            bridge = _bridge_for(srv, self)
            if bridge is None:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            return self._json(bridge.cheat_library_status())
        # ^ MUST stay above the prefix route below. /api/cheats/<title-id> is a startswith match,
        # so it swallowed /api/cheats/library and answered it as a title whose id is the literal
        # string "library" - {"title_id":"library","cheats":[],"patches":[]}.
        # Same trap the /api/cred vs /api/credscan pair hit before.
        #
        # Neither of these two is read by the page today (index.html calls /api/cheats/paths,
        # /api/cheats/rescan and /api/mods/*); they stay for curl and the tools. Note that
        # /api/cheats/<tid> below lists the LEGACY CheatRunner tree (config cheats.root), not the
        # ELF's own library at CONSOLE_CHEAT_ROOT - the mods panel goes through /api/mods/.
        if path.startswith("/api/cheats/"):
            tid = unquote(path[len("/api/cheats/"):])
            b = _bridge_for(srv, self)
            res = b.console_cheats(tid) if b else None
            return self._json({"title_id": tid, "reachable": res is not None,
                               "cheats": (res or {}).get("cheats", []), "patches": (res or {}).get("patches", [])})
        if path == "/api/payloads":
            return self._payloads_list(q)
        if path == "/api/payloads/updates":
            return self._payloads_updates(q)
        if path == "/api/queue":
            return self._json({"tasks": srv.queue.snapshot()})
        if path == "/api/dpi/reload":                # the dock's manual reload button
            # It clears the console's in-flight install latch - the guard whose clearing is how
            # the duplicate-install panic happened - so a hostile page must not be able to fire it.
            if not self._origin_ok():
                return self._refuse_cross_site()
            return self._dpi_reload()
        # GET twins of the controls above. The on-console build is GET-only, so serving both
        # verbs here lets one UI call work whether the page came from the PC or from the PS5 itself.
        return self._static(path.lstrip("/"))

    def _refuse_cross_site(self):
        return self._json({"ok": False, "error": "forbidden_origin",
                           "message": "Cross-site request refused."}, 403)

    # ---------------------------------------------------------------- request origin guard
    # WHY THIS EXISTS. Both servers answer with Access-Control-Allow-Origin: * and neither has any
    # authentication - deliberately, because everything on a jailbroken console is open and
    # Payload Manager on :8084 will load an arbitrary kernel-privileged ELF for anyone who asks.
    # Hardening against someone already ON the LAN is therefore mostly theatre.
    #
    # What is NOT theatre is a page on the public internet. Any site the user visits, in any
    # browser on this network, can fire a cross-origin request at http://<this-pc>:8710/... and
    # our handlers would run it. That needs no LAN access and no knowledge of the user - just a
    # visit to a hostile page while the companion is running.
    #
    # The discriminator that works here is NOT "same origin" - the console serves the UI from
    # 10.0.0.99 and legitimately calls this PC cross-origin, which is the whole architecture. It
    # is whether the caller's origin is on the LAN AT ALL. A hostile public site has a DNS
    # hostname; every legitimate caller here is a private-range IP literal or loopback.
    #
    # Absent Origin AND Referer is allowed on purpose: that is curl, our own tooling, and
    # server-to-server calls, none of which a remote page can forge.
    #
    # But a browser CAN be made to send neither - a page with a no-referrer policy loading a
    # state-changing GET as an <img>, a <script> or a top-level navigation - and "Origin: null"
    # from a sandboxed frame is not a name either. So the browser's own statement of HOW the
    # request was made is consulted too: Sec-Fetch-Mode and Sec-Fetch-Dest cannot be set or
    # suppressed by a page. A navigation, a subresource load or a no-cors fetch is refused;
    # ABSENT headers stay allowed (curl, urllib, the console's own calls, older WebKit), and
    # everything the UI does is fetch() in mode "cors" or "same-origin" with an empty dest, so
    # nothing the app itself does changes. Mirrors sec_fetch_ok() in server.c.
    #
    # Private ranges: RFC 1918, loopback, link-local (169.254 - APIPA when DHCP fails) and the
    # CGNAT block 100.64/10 that Tailscale hands out - none of them routable from the internet,
    # so none can be a hostile page's origin. A name is accepted in exactly one case: when it is
    # this server's own Host header, which makes the page first-party by definition - browsing
    # the PC as http://asus-lap:8710 used to 403 every button while the page loaded fine.
    _PRIVATE_HOST = re.compile(
        r"^(?:localhost|127\.\d+\.\d+\.\d+|10\.\d+\.\d+\.\d+|192\.168\.\d+\.\d+"
        r"|172\.(?:1[6-9]|2\d|3[01])\.\d+\.\d+|169\.254\.\d+\.\d+"
        r"|100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d+\.\d+|\[::1\]|::1)$")
    _BAD_FETCH_MODES = ("no-cors", "navigate", "nested-navigate")
    _BAD_FETCH_DESTS = ("image", "script", "style", "iframe", "frame", "object", "embed",
                        "font", "video", "audio")
    _refused_logged = set()

    def _refuse_log(self, why):
        # Once per distinct reason, so a 403 is diagnosable from pms.log without flooding it.
        if why not in Handler._refused_logged and len(Handler._refused_logged) < 64:
            Handler._refused_logged.add(why)
            print("[origin] refused %s %s: %s" % (self.command, self.path.split("?")[0], why))

    def _origin_ok(self):
        mode = (self.headers.get("Sec-Fetch-Mode") or "").strip().lower()
        dest = (self.headers.get("Sec-Fetch-Dest") or "").strip().lower()
        if mode in self._BAD_FETCH_MODES or dest in self._BAD_FETCH_DESTS:
            self._refuse_log("Sec-Fetch-Mode=%s Sec-Fetch-Dest=%s" % (mode or "-", dest or "-"))
            return False
        try:
            own = (urlparse("//" + (self.headers.get("Host") or "").strip()).hostname or "").lower()
        except ValueError:
            own = ""
        for hdr in ("Origin", "Referer"):
            v = (self.headers.get(hdr) or "").strip()
            if not v or v == "null":
                continue
            try:
                host = urlparse(v).hostname or ""
            except ValueError:
                self._refuse_log("%s header could not be parsed" % hdr)
                return False
            if self._PRIVATE_HOST.match(host):
                return True         # first present header decides
            if own and host.lower() == own:
                return True         # our own page, by whatever name it was opened under
            self._refuse_log("%s host %r is not on this network" % (hdr, host))
            return False            # a public site is talking to us - refuse
        return True                 # no Origin and no Referer: not a browser page

    def do_POST(self):
        return self._guarded(self._do_POST)

    def _do_POST(self):
        path, srv = urlparse(self.path).path, self.server
        if not self._origin_ok():
            return self._refuse_cross_site()
        body = self._body()

        # ---- mods / cheats / patches — OUR Mutant engine first, CheatRunner only as a fallback ----
        if path == "/api/engine/agent":
            # Listing or unlisting the in-game helper changes what loads into every game on that
            # console, so it is a POST and it carries the same cross-site guard as everything else
            # that changes a console.
            if not self._origin_ok():
                return self._refuse_cross_site()
            b = _bridge_for(srv, self, body)
            if not b:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            # THE KEY IS MANDATORY HERE TOO. `1 if enabled else 0` turns a missing or misspelled
            # field into an explicit {"enabled": 0}, which is a PURGE - so the console's own refusal
            # would never be reached and a malformed request would quietly wipe a deliberate arm.
            want = body.get("enabled")
            if want not in (0, 1, True, False):
                return self._json({"ok": False, "error": "This request has to say \"enabled\":0 or "
                                                         "\"enabled\":1 - it is not guessed"}, 400)
            return self._json(b.engine_agent_set(
                want,
                title=(str(body.get("title") or "").strip() or None),
                all_games=bool(body.get("all_games"))) or
                {"ok": False, "error": "console did not answer"})
        mm = re.match(r"^/api/mods/([^/]+)/(select|toggle|apply|disable-all)$", path)
        if mm:
            tid, act = unquote(mm.group(1)), mm.group(2)
            # NO `want` HERE, so a read and a write of the same panel can never land on different
            # consoles: the GET twin above has never had one, and this asked for a PS5. On a fleet
            # whose first entry is the PS4 the panel read one machine and wrote to the other; on a
            # PS4-ONLY setup _bridge_for answered None and this returned 400 "no console
            # configured", which the page paints as "can't reach the mod engine" - about a console
            # that was configured, online and listed in Settings. A PS4 refuses mods itself, in its
            # own words, and that is the honest reply. The ordering fix in reconcile_consoles keeps
            # consoles[0] the PS5 on a fleet that has one, so a caller that names no console still
            # resolves to exactly the console want="ps5" used to pick.
            b = _bridge_for(srv, self, body)
            if not b:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            body = body or {}
            force = bool(body.get("force"))

            # This block used to open with `if not cr.alive(): return cheatrunner_offline`.
            # That gate ran BEFORE anything else, so every toggle from the PC answered
            # "CheatRunner is not running on the PS5 (:9999)" — on a console whose own ELF
            # implements the entire engine and had already listed the mods on the GET above.
            # The mods panel could read state but never change it. Our engine goes first now,
            # exactly like the GET path, and CheatRunner stays what the comment always claimed
            # it was: a last resort, never a requirement.
            mine = b.mods_action(tid, act, body)
            if not isinstance(mine, dict):
                mine = None
            if mine is not None:
                # Our engine answered — including its own honest refusals ("game_not_running",
                # or a refusal to write because the game's code does not match the cheat file).
                # Only an outright "I have no cheat file for this title" is worth asking
                # CheatRunner about, and only if it happens to be running.
                if not (mine.get("error") == "no_local_cheat_found" and b.cheats.alive()):
                    return self._json(mine)

            cr = b.cheats
            if not cr.alive():
                return self._json({"ok": False, "error": "engine_unreachable",
                                   "message": "The %s app is not answering, so mods cannot be "
                                              "changed. Load PKG-MUTANT-SHOP.elf on the console "
                                              "and try again." % b.kind_label()}, 503)
            if act == "select":
                # Refuse a version-mismatched file unless explicitly forced: cheats built for
                # another build are the usual cause of in-game crashes.
                fv = cheat_file_version(body.get("file") or body.get("path") or "")
                iv = ""
                for a in (b.console_apps() or []):
                    if a.get("title_id") == tid:
                        iv = a.get("app_ver") or ""
                        break
                if fv and iv and fv != iv and not force:
                    return self._json({"ok": False, "error": "version_mismatch",
                                       "file_version": fv, "installed_version": iv,
                                       "message": "This cheat file is for v%s but v%s is installed. "
                                                  "Loading it anyway can crash the game." % (fv, iv)}, 409)
                r = cr.select(tid, body.get("path") or "", force=force)
            elif act == "toggle":
                mine = b.mutant_running()
                if mine and mine.get("title_id") == tid and mine.get("cheat_file"):
                    # our engine: writes the bytes itself and toasts on the TV
                    r = b.mutant_apply(mine["cheat_file"], int(body.get("index", 0)),
                                       bool(body.get("on")), mine["pid"],
                                       int(str(mine["base"]), 16), body.get("name") or "",
                                       force=force)
                    return self._json(r if isinstance(r, dict) else {"ok": False})
                r = cr.toggle(tid, int(body.get("index", 0)), bool(body.get("on")))
                if isinstance(r, dict) and r.get("ok"):
                    b.notify("%s: %s" % ("ON" if body.get("on") else "OFF",
                                         (body.get("name") or "Mod")[:56]))
            elif act == "apply":
                r = cr.apply_patch(tid, index=body.get("index"), entry_id=body.get("entry_id"),
                                   force=force)
                if isinstance(r, dict) and r.get("ok"):
                    b.notify("Patch applied: %s" % (body.get("name") or tid)[:56])
            else:
                r = cr.disable_all(tid)
                if isinstance(r, dict) and r.get("ok"):
                    b.notify("All mods disabled")
            return self._json(r if isinstance(r, dict) else {"ok": False})
        if path == "/api/config":
            srv.cfg = coerce_config(deep_merge(srv.cfg, body))
            save_config(srv.cfg)
            srv.library.cfg = srv.cfg
            srv.fleet.cfg = srv.cfg
            # deep_merge returns a NEW dict, so anything still holding the old one keeps reading
            # pre-save values. The queue was the one that got missed - it bound self.cfg when it
            # was constructed, so integrity and parallelism settings only took effect on restart.
            srv.queue.cfg = srv.cfg
            srv.fleet.reload()
            srv.engine.set_sources(assemble_sources(srv.cfg))
            return self._json({"ok": True})
        if path == "/api/ps4/tile":
            # Install (or reinstall, with force) the PS4 dashboard app on the chosen console.
            cid = body.get("console") or ""
            b = srv.fleet.bridge(cid) if cid else None
            if b is None:
                b = _ps4_bridge(srv)
            if b is None:
                return self._json({"ok": False, "error": "No PS4 is set up"}, 400)
            ok, info = b.install_ps4_tile(force=bool(body.get("force")))
            out = {"ok": bool(ok), "title_id": PS4_TILE_TID}
            if isinstance(info, dict):
                out.update({k: v for k, v in info.items() if k in ("error", "already", "busy", "rc")})
            return self._json(out, 200 if ok else 502)
        if path == "/api/rescan":
            srv.library.scan()
            return self._json({"ok": True, "count": len(srv.library.games), "empty": srv.library.is_empty,
                               "gen": srv.library.gen, "last_scan_ms": srv.library.last_scan_ms})
        if path == "/api/library/prepare":                              # [B6] create Games/PS4|PS5 tree
            root = (body.get("root") or "").strip()
            if not root:
                paths = srv.cfg.get("library", {}).get("local_paths", [])
                root = paths[0] if paths else ""
            if not root:
                return self._json({"ok": False, "error": "no library root — set library.local_paths or pass root"}, 400)
            res = ensure_library_tree(root)
            if res.get("ok"):
                srv.library.scan()
            return self._json(res)
        if path == "/api/open-folder":
            opened = []
            for p in srv.cfg["library"].get("local_paths", []):
                if not os.path.isdir(p):
                    continue
                try:
                    if sys.platform == "win32":
                        os.startfile(p)                       # opens Explorer on the companion PC
                    elif sys.platform == "darwin":
                        subprocess.Popen(["open", p])
                    else:
                        subprocess.Popen(["xdg-open", p])
                    opened.append(p)
                except Exception as e:
                    print("[open-folder] %s" % e)
            return self._json({"ok": True, "opened": opened})
        if path == "/api/notify":
            b = _bridge_for(srv, self, body)
            return self._json({"ok": bool(b and b.notify(body.get("text", "")))})
        if path == "/api/move":
            # A console-owned operation: it copies the container between the console's OWN drives,
            # verifies, then removes the original. The companion only forwards - it has no business
            # knowing console paths, and the console can finish the job with this PC switched off.
            b = _bridge_for(srv, self, body)
            if b is None:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            try:
                return self._json(b._shop("/api/move", timeout=30, data={
                    "path": body.get("path") or "",
                    "drive": body.get("drive") or "",
                    "name": body.get("name") or "",
                }) or {})
            except ShopHTTPError as e:
                return self._json({"ok": False, "error": e.sentence("The console refused the move")}, 502)
            except Exception:
                return self._json({"ok": False,
                                   "error": "PKG MUTANT SHOP on the PS5 did not answer - load it "
                                            "again from Payload Manager"}, 502)
        if path == "/api/game/delete":
            # Remove a PS5 backup container from THE CONSOLE. Proxied, never reimplemented: the
            # console owns the scan that knows where the container is, and keeping the lookup there
            # means this PC never sends a path - so there is nothing here that could be pointed at
            # the local library by accident.
            b = _bridge_for(srv, self, body)
            if b is None:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            tid = (body.get("title_id") or "").strip()
            if not tid:
                return self._json({"ok": False, "error": "which game? no title id was given"}, 400)
            try:
                r = b._shop("/api/game/delete-backup?tid=%s" % quote(tid, safe=""), timeout=60) or {}
            except ShopHTTPError as e:
                return self._json({"ok": False, "error": e.sentence("The console refused")}, 502)
            except Exception as e:
                return self._json({"ok": False,
                                   "error": "PKG MUTANT SHOP on the PS5 did not answer - load it "
                                            "again from Payload Manager",
                                   "detail": repr(e)[:200]}, 502)
            if r.get("deleted"):
                # The library's record of it is now wrong in two places; make both re-read.
                # invalidate_apps() first: it drops the 30 s backup memo, so the deleted
                # container leaves the folded-in size/path now rather than half a minute later.
                try:
                    b.invalidate_apps()
                    b.installed_titles(force=True)
                except Exception:
                    pass
                srv.library.scan()
            return self._json(r)
        if path == "/api/open-ps5":
            b = _bridge_for(srv, self, body)
            if b is None:
                return self._json({"ok": False,
                                   "error": "no console configured"}, 400)
            # Default to OUR address, not the console's loopback: opening 127.0.0.1 on the console
            # gives the on-console UI, which cannot install anything that lives on this PC.
            # companion_ip_for(), not lan_ip(): on a dual-homed PC the default route's address
            # can be one the console has no route to - the same trap the install URL avoids.
            url = body.get("url") or ("http://%s:%d/" % (companion_ip_for(b.ip),
                                                         srv.cfg["companion"]["port"]))
            return self._json(b.open_shop(url))
        if path == "/api/transfer":
            # Pull a game from another PC into this one's library folder.
            key = body.get("install_key")
            if not key:
                return self._json({"ok": False, "error": "need install_key"}, 400)
            # ...unless a different PC was chosen as the destination. The copy has to be run BY the
            # machine that is going to hold the file, so hand the same request to it. Without this
            # the only possible destination was whichever PC you happened to have the app open on.
            dest_pc = body.get("to") or body.get("dest_pc")
            if dest_pc:
                me = device_identity(srv.cfg)
                mine = {str(me.get("id")), str(me.get("name")), lan_ip(), "local", "this"}
                if str(dest_pc) not in mine:
                    reg = getattr(srv, "peers", None)
                    target = None
                    for p in (reg.known() if reg is not None else []):
                        if str(dest_pc) in (str(p.get("id")), str(p.get("name")), str(p.get("lan_ip"))):
                            target = p
                            break
                    if not target or not target.get("url"):
                        return self._json({"ok": False, "error": "unknown destination PC %r" % dest_pc}, 404)
                    fwd = dict(body)
                    fwd.pop("to", None)
                    fwd.pop("dest_pc", None)
                    try:
                        rq = urllib.request.Request(
                            target["url"].rstrip("/") + "/api/transfer", method="POST",
                            data=json.dumps(fwd).encode("utf-8"),
                            headers={"Content-Type": "application/json"})
                        with urllib.request.urlopen(rq, timeout=25) as rr:
                            ans = json.loads(rr.read().decode("utf-8", "replace"))
                        ans["on_pc"] = target.get("name")
                        return self._json(ans)
                    except Exception as e:
                        return self._json({"ok": False,
                                           "error": "%s did not take the copy: %s"
                                                    % (target.get("name") or dest_pc, e)}, 502)
            item = src_game = None
            for cand in build_federated_library(srv).get("games", []):
                for it in (cand.get("base") or []) + (cand.get("updates") or []) + (cand.get("dlc") or []):
                    if it.get("install_key") == key and it.get("peer_url"):
                        item, src_game = it, cand
                        break
                if item:
                    break
            if not item:
                return self._json({"ok": False, "error": "that game is not on another PC "
                                                        "(or it is already here)"}, 404)
            plat = (src_game.get("platform") or "PS4").upper()
            root = srv.cfg.get("library", {}).get("root") or LIBRARY_ROOT
            dest = os.path.join(root, plat if plat in LIBRARY_LAYOUT else "")
            job = srv.queue.add({"name": src_game.get("name") or item.get("file"),
                                 "title_id": src_game.get("title_id"), "kind": item.get("kind", "base"),
                                 # NO CONSOLE IS INVOLVED IN THIS. Stamping the copy with
                                 # consoles[0] did two things, both wrong: the queue serialises one
                                 # job per console id, so a 100 GB PC-to-PC copy blocked every
                                 # install for that console for as long as it ran - rows sitting at
                                 # "Queued" with the console idle - and the row itself was labelled
                                 # with a console that had nothing to do with it (the PS4, on a
                                 # fleet whose first entry is one). An empty id is its own lane:
                                 # _claim/_release serialise copies against each other, never
                                 # against a real console, and the row renderer drops the console
                                 # chip when there is no id to print. Nothing downstream looks up a
                                 # bridge for this lane - _run dispatches pc-copy before it resolves
                                 # one, and _finished_installing is never called here.
                                 "lane": "pc-copy", "console": "",
                                 "url": item["peer_url"], "dest": dest,
                                 "file": item.get("file") or os.path.basename(item["peer_url"]),
                                 "from_pc": src_game.get("source_pc"),
                                 "total": item.get("size") or 0,
                                 "hold": body.get("mode") == "queued"})
            return self._json({"ok": True, "id": job, "lane": "pc-copy",
                               "from": src_game.get("source_pc"), "dest": dest})
        if path == "/api/payloads/update":
            return self._payloads_update(body)
        if path == "/api/app/update":
            # FETCH AND INSTALL A NEWER BUILD OF THIS APP, NOW. The same thing the background check
            # does on its own clock, for somebody who does not want to wait for it. It installs
            # nothing on a console and never restarts on its own - that is the next route.
            ok, msg = self_update_once(srv.cfg, force=True, log=lambda m: print(m))
            return self._json({"ok": ok, "message": msg, "update_ready": app_update_state()})
        if path == "/api/app/restart":
            # THE OWNER PICKING THE MOMENT. Only ever offered once a newer build is actually on
            # disk, because restarting into the same version is a pointless interruption - and the
            # panel is where it is offered from, so the request can come from a console or a phone.
            st = app_update_state()
            # A COPY THAT DID NOT UNPACK PROPERLY IS THE ONE CASE WHERE RESTARTING INTO THE SAME
            # VERSION IS THE WHOLE POINT - a fresh launch unpacks itself again, which is the fix.
            if not st.get("version") and not _degraded:
                return self._json({"ok": False,
                                   "message": "There is no newer build waiting."}, 400)
            if not payload_engine.running_exe():
                return self._json({"ok": False,
                                   "message": "This is not the packaged app."}, 400)
            if relaunch_self(srv.cfg["companion"]["port"], log=lambda m: print(m)):
                return self._json({"ok": True, "restarting": True,
                                   "message": ("Restarting into %s." % st["version"])
                                   if st.get("version") else "Restarting this build."})
            return self._json({"ok": False, "message": "The new build would not start."}, 500)
        if path in ("/api/payloads/send", "/api/payloads/seed", "/api/payloads/install",
                    "/api/payloads/run"):
            return self._payloads_act(path.rsplit("/", 1)[-1], body)
        if path == "/api/install":
            return self._install(body)
        if path == "/api/hosts/cleanup":             # kept as a no-op: see the reply
            bridge = _bridge_for(srv, self, body, want="ps5")
            if bridge is None:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            # There is nothing to clean up. This used to stop Elf Arsenal and the daemons it
            # spawned so they would stop fighting etaHEN for :12800. This app no longer ships,
            # starts or talks to either, and it binds nothing on that port itself - so if
            # something IS on 12800 it is a payload the user loaded, and stopping it is their
            # decision, not ours.
            return self._json({"ok": True, "stopped": [], "survived": [],
                               "install_host": "our own engine",
                               "message": "Nothing to do - this app does not use a separate "
                                          "install payload any more."})
        if path == "/api/cheats/sync":               # push only what the console is missing
            bridge = _bridge_for(srv, self, body)
            if bridge is None:
                return self._json({"ok": False, "error": "no console configured"}, 400)
            return self._json(bridge.sync_cheat_library(
                log=lambda m: print("[cheats] %s" % m),
                budget=float(body.get("budget_seconds") or 0) or None))
        if path == "/api/queue/clear":
            srv.queue.clear_done()
            return self._json({"ok": True})
        if path == "/api/queue/start":                                  # [B8] release held tasks, small-first
            return self._json({"ok": True, "started": srv.queue.start_held()})
        if path == "/api/dpi/reload":                # POST twin of the GET above
            return self._dpi_reload(body)
        # /api/dpi/status was here. It reported whether a third-party install daemon was clean,
        # wedged or down. Nothing in the UI ever called it and there is no such daemon now.
        m = re.match(r"^/api/queue/([^/]+)/cancel$", path)
        if m:
            return self._json({"ok": srv.queue.cancel(m.group(1))})
        m = re.match(r"^/api/queue/([^/]+)/dismiss$", path)
        if m:
            return self._json({"ok": srv.queue.dismiss(m.group(1))})
        m = re.match(r"^/api/queue/([^/]+)/retry$", path)               # [wedge-retry]
        if m:
            return self._json({"ok": srv.queue.retry(m.group(1))})
        return self._json({"error": "not found"}, 404)

    # ---------------------------------------------------------- Payloads & Homebrews ----------
    def _pl_platform_of(self, bridge):
        if bridge is None:
            return ""
        try:
            return "PS4" if bridge.is_ps4() else "PS5"
        except Exception:
            return ""

    def _pl_find(self, ident, plat):
        """The item a press means, read from the folder as it is NOW.

        THIS USED THE SHIPPED CATALOGUE AND THE LIST ROUTE USED THE LIVE ONE, so the moment the
        update button replaced webkit-autoloader-installer_v0.5.1.elf with _v0.5.2.elf the panel
        showed 0.5.2 and pressing it answered "that file is not on this PC" - because the lookup was
        still describing a filename that no longer existed. Two views of one folder is how that
        happens; there is one now.
        """
        want = str(plat or "").upper()
        cat, _sig = payload_engine.live_catalog(self.server.cfg, WEB_DIR)
        for it in (cat.get("items") or []):
            if it.get("id") == ident and str(it.get("platform") or "").upper() == want:
                return it
        return None

    def _payloads_list(self, q):
        """Everything the panel draws, with state that was OBSERVED rather than assumed.

        Three different states, and they are not interchangeable:
          here       the bytes are on THIS PC. The exe runs on machines that never had the folder.
          live       a port belonging to this payload answered. None means nothing listens for it,
                     so there is no way to tell - which the tile says, instead of showing a green
                     light nobody can back.
          installed  a homebrew is a title this console already has, read from the console itself.
        """
        srv = self.server
        b = _bridge_for(srv, self)
        plat = self._pl_platform_of(b)
        ip = getattr(b, "ip", None) if b is not None else None
        # THE FOLDER AS IT IS NOW, not as it was when this build was made. A payload the update
        # button just replaced, or a file dropped in while the panel is open, is in this answer.
        # It costs one stat per catalogued file when nothing has changed.
        cat, fsig = payload_engine.live_catalog(srv.cfg, WEB_DIR)
        items = [dict(it) for it in (cat.get("items") or [])
                 if not (False)]

        # ONE batched probe, for this console's ports only. The PS4's :9090 is never in here: a
        # bare connect to GoldHEN's loader stops it listening, and the POST of an ELF is the probe.
        ports = set()
        for it in items:
            if str(it.get("platform") or "").upper() == plat:
                try:
                    p = int(it.get("port") or 0)
                except (TypeError, ValueError):
                    p = 0
                if p:
                    ports.add(p)
        # ONE ROUND OF PROBES PER FOUR SECONDS, HOWEVER MANY VIEWERS THERE ARE. The panel polls to
        # stay current, and every poll used to cost a port sweep plus a Payload Manager call plus a
        # console app list - per open page. The panel is not worth that; the console's accept loop
        # certainly is not (the PS5 answers on one). The cache is keyed on the console so a fleet
        # does not share an answer.
        _ck = "%s|%s" % (plat, ip or "-")
        _now = time.time()
        _pc = getattr(srv, "_pl_state_cache", None)
        if _pc is None:
            _pc = srv._pl_state_cache = {}
        _hit = _pc.get(_ck)
        # THE AGE IS THE AGE OF THE ANSWER, NOT OF THE LAST GLANCE AT IT. This stored the tuple
        # again at the bottom with a fresh `_now` whatever had happened - including on a hit, where
        # the values were simply copied out of the previous entry. So every poll reset the clock on
        # data nobody had re-read, and because the panel polls every three seconds and this expires
        # after four, the clock was reset before it could ever run out. The console was asked once,
        # when the panel was first opened, and never again for as long as somebody kept it open.
        #
        # That is how the owner's PS4 tile went on saying FPKGi was "Installed" after it had been
        # deleted: measured here, this PC answered installed=True for PKGI13337 while the console's
        # own /api/library did not list the title at all, and the second PC - which nobody was
        # watching, so its entry expired - answered correctly. It froze `live` and `running` and
        # the console's reported version in exactly the same way.
        _hit = probe_cache_hit(_pc, _ck, _now)
        _fresh = _hit is None
        if not _fresh:
            live, running, installed = _hit[1], _hit[2], _hit[3]
            cst = _hit[4] if len(_hit) > 4 else None
        else:
            live = payload_engine.live_ports(ip, ports) if ip else set()
            running, installed, cst = None, None, None
            _pc[_ck] = None      # replaced below once the two console reads are done

        # THE PROCESS LIST IS THE TRUTH, AND A PORT IS ONLY A SECOND OPINION. Measured on this
        # console: ShadowMountPlus runs happily WITHOUT binding 10101, so a port probe reported it
        # as stopped while pldmgr listed it at pid 104 - the same shape of wrong answer the 9021
        # mix-up gave for months. Payload Manager can see kstuff and nanodns too, which nothing
        # listens for at all, so this is the difference between the panel knowing and guessing.
        #
        # Only the PS5 has a Payload Manager. The PS4 has GoldHEN's loader, which cannot be asked
        # anything - and must never be connected to just to ask.
        try:
            if not _fresh:
                pass
            else:
                # ASK THE CONSOLE FIRST. It can see things this PC cannot: a UDP service whose port
                # is simply taken (nanodns answers nobody), and on a PS5 everything Payload Manager
                # has in its process list. A PS4 has no process list at all, so without this its
                # nanodns is invisible - which is exactly what the owner reported after starting it
                # from this panel and watching the tile stay grey.
                cst = payload_engine.console_state(ip)
                running = (cst or {}).get("live")
                if running is None and b is not None and plat != "PS4":
                    pm = b.pldmgr
                    if callable(pm):
                        pm = pm()
                    procs = pm.processes() if pm else []
                    if procs:
                        # STEMS, NOT FILENAMES. pldmgr reports the name a payload was BUILT as:
                        # ftpsrv-ps5.elf runs as ftpsrv.elf and pldmgr_v0.5.2.elf as pldmgr.elf.
                        running = {payload_engine.proc_stem(p.get("name"))
                                   for p in procs if p.get("name")}
        except Exception:
            running = None

        try:
            if not _fresh:
                pass
            elif b is not None:
                # INSTALLED HAS TO MEAN INSTALLED. installed_titles() is every title the console's
                # app list MENTIONS, which is the right answer for the install-confirm loop that
                # waits for a row to appear - and the wrong one here. A console lists titles it does
                # not have: measured on the owner's PS4, 21 titles of which 5 (THE PLAYROOM, Game
                # Streaming, Share Play, PlayStation Video, PS5 Remote Play) are not installed, and
                # a deleted app keeps its row for a while before the console drops it. Every one of
                # those would paint as "Installed" in this panel.
                #
                # install_status is 0 for a title whose data is really in place, on both consoles -
                # the PS5 reads it from app.db and the PS4 path sets it from the payload's own
                # `installed` flag - so it is the one field that answers the question being asked.
                installed = installed_ids(b.console_apps())
        except Exception:
            installed = None
        if _fresh:
            _pc[_ck] = (_now, live, running, installed, cst)

        root = payload_engine.source_root(srv.cfg)
        # WHO COULD HAND THIS OVER. "Not on this PC" was being treated as "cannot be used", which is
        # wrong the moment a second machine runs this exe: the console carries every payload itself,
        # and another companion may hold the folder. The tile needs to know the difference between
        # "nobody has it" and "this particular PC does not".
        try:
            _reg = getattr(srv, "peers", None)
            _peers = _reg.known() if _reg is not None else []
        except Exception:
            _peers = []
        _chave = (cst or {}).get("have") or {}
        _ckept = (cst or {}).get("kept") or {}
        for it in items:
            same = str(it.get("platform") or "").upper() == plat
            _local = payload_engine.local_path(srv.cfg, it) is not None
            _on_console = False
            if same:
                if it.get("kind") == "payload":
                    _on_console = payload_engine.proc_stem(it.get("file")) in _chave
                else:
                    _on_console = int(it.get("size") or 0) in _ckept
            _peer = None if _local else payload_engine.peer_with(_peers, it)
            it["here"] = bool(_local or _on_console or _peer)
            it["from"] = ("pc" if _local else "console" if _on_console
                          else ("peer" if _peer else ""))
            it["from_name"] = (_peer or {}).get("name", "") if _peer else ""
            try:
                p = int(it.get("port") or 0)
            except (TypeError, ValueError):
                p = 0
            # A payload loaded some other way can appear under Payload Manager's generic
            # "payload.elf", so a name that IS in the list proves it runs, while a name that is not
            # only means "not under its own name". That is why absence is reported as False when we
            # actually have a list to look in, and as None - no way to tell - when we do not.
            fn = payload_engine.proc_stem(it.get("file"))
            if same and running is not None and fn and fn in running:
                it["live"] = True
            elif same and p and ip and p in live:
                it["live"] = True
            elif same and it.get("kind") == "payload" and (running is not None or (p and ip)):
                it["live"] = False
            else:
                it["live"] = None
            if it.get("kind") == "homebrew":
                tid = str(it.get("title_id") or "").upper()
                it["installed"] = ((tid in installed)
                                   if (same and tid and installed is not None) else None)
        # THE SIGNATURE IS WHAT MAKES AUTO-REFRESH CHEAP. It covers the folder, what is running and
        # what is installed, so the page can repaint only when one of them actually moved instead of
        # rebuilding a grid of live buttons every few seconds.
        state_sig = "%s|%s|%s|%d" % (
            fsig, ",".join(sorted(str(p) for p in live)),
            ",".join(sorted(running or [])), len(installed or ()))
        return self._json({"ok": True, "platform": plat,
                           "console": (b.c.get("id") if b is not None else ""),
                           # WHICH BUILD IS ON THAT CONSOLE. Our own tile cannot read this out of
                           # the catalogue: the catalogue records what the owner's folder held when
                           # the ELF was built, so it is one release behind by construction.
                           "console_version": (cst or {}).get("version", ""),
                           # A newer build of THIS app already on disk, waiting for a restart. The
                           # panel polls this every few seconds, so the banner appears by itself.
                           "update_ready": app_update_state(),
                           # ...and the opposite: this copy came up without its own files. Said here
                           # because this is the route the panel builds itself from.
                           "degraded": _degraded,
                           "root": root, "source_here": os.path.isdir(root),
                           "sig": hashlib.sha256(state_sig.encode("utf-8")).hexdigest()[:16],
                           "items": items})

    def _payloads_updates(self, q):
        """What upstream has, for the items that name an upstream.

        One call per repo, short timeout, no retry: this is the only thing in the app that leaves
        the LAN, so a firewall that drops it costs one greyed line and never a slow panel.
        """
        force = (q.get("force") or [""])[0] in ("1", "true", "yes")
        cat, _sig = payload_engine.live_catalog(self.server.cfg, WEB_DIR)
        rels, out = {}, []
        for it in (cat.get("items") or []):
            repo = it.get("repo")
            # OUR OWN ENTRY IS CHECKED NOW, and it is the point of this whole lane: the app's tile
            # in this panel is where the owner - and anyone running the exe at home - is told a new
            # release exists and can take it. `ours` was skipped here because our entry had no
            # upstream to ask; it has one now (XavyProd-Git/pkg-mutant-shop), so the same machinery
            # that offers a new ftpsrv offers a new shop, down to verifying the marker INSIDE the
            # download before anything is replaced.
            if not repo:
                continue
            if repo not in rels:
                rels[repo] = payload_engine.github_latest(
                    repo, force=force, token=payload_engine.gh_token(self.server.cfg))
            rel = rels[repo]
            asset = payload_engine.pick_asset(rel, it)
            have = it.get("version") or ""
            tag = rel.get("tag") or ""
            # A VERSION WE CAN PROVE, FOR THE ONES THAT CARRY NONE. ftpsrv, nanodns and
            # ShadowMountPlus write no version into their binaries and none into their filenames,
            # so the catalogue honestly says "unknown" - but if the file on disk is byte-for-byte
            # the size of the asset in a release, it IS that release. Measured: the owner's
            # ftpsrv-ps4.elf is 166,072 bytes and so is 1.16-ng-stable's. That is evidence, not a
            # guess, and it is reported as coming from the release rather than from the file.
            from_release = ""
            if not have and asset and int(asset.get("size") or 0) == int(it.get("size") or -1):
                have = from_release = tag
                # ...and remember it, so the next build ships the answer and a console with no PC
                # and no internet can still name this payload's version.
                payload_engine.remember_version(it, tag, repo, asset.get("name") or "")
            # AN ITEM, NOT A REPO. One project can supply both consoles (ftpsrv does), and the two
            # files update independently - so the answer is per item, with the asset that would
            # actually replace THIS file. A release with no matching file is not an update.
            # OUR OWN ROW ALSO ANSWERS FOR THE APP DOING THE ASKING. `have` above is the version
            # of the ELF in the owner's folder, which is the right answer for every other payload
            # and only half of ours: a PC whose folder was already current reported "everything
            # matches" while running a build three releases old, and the only way out was copying
            # the exe across by hand. The row is offered when EITHER half is behind, and says which.
            app_have, app_newer = "", False
            if it.get("ours") and payload_engine.running_exe():
                app_have = VERSION
                app_newer = payload_engine.newer_than(tag, VERSION)
            # WHY THERE IS NO UPDATE, WHEN THERE IS NO UPDATE. Every one of these was silent: the
            # panel draws only rows where `newer` is true, so an item this lane cannot form an
            # opinion about looks exactly like one that is current. The owner was reading "nothing
            # to do" off a panel that had simply failed to compare anything, while newer builds sat
            # on the projects' own pages. None of these are errors - they are answers.
            why = ""
            if rel.get("error"):
                why = ""                              # already reported as an error, not a reason
            elif not asset:
                why = ("no-asset" if (rel.get("assets") or [])
                       else "no-files")               # a release with nothing attached to it
            elif not have:
                why = "unknown-version"               # nothing to compare the upstream tag against
            elif from_release:
                why = ""                              # proven identical to the release: current
            elif (not payload_engine.newer_than(tag, have)
                  and payload_engine.version_tuple(tag) != payload_engine.version_tuple(have)):
                # Two versions that genuinely do not compare - Itemzflow's PS5 package says 1.14
                # while its releases say 1.08, which is two numbering systems rather than an
                # upgrade path. Compared as NUMBERS, not as strings: "v0.5.2" and "0.5.2" are the
                # same release and reporting them as incomparable was this check's own first bug.
                why = "cannot-compare"
            out.append({
                "app_have": app_have, "app_newer": app_newer,
                "id": it.get("id"), "platform": it.get("platform"),
                "title": it.get("title"), "repo": repo,
                "have": have, "tag": tag, "have_from": ("release" if from_release
                                                        else (it.get("version_from") or "")),
                "url": rel.get("url", ""), "published": rel.get("published", ""),
                "error": rel.get("error", ""),
                "asset": (asset or {}).get("name", ""),
                "size": (asset or {}).get("size", 0),
                # The reason this row offers nothing, for the panel to show instead of silence.
                "why": why,
                # What the project itself calls its newest, and whether that is a pre-release -
                # /releases/latest hides those, and hiding them is what started all of this.
                "prerelease": bool(rel.get("prerelease")),
                "stable_tag": rel.get("stable_tag", ""),
                # If the file we hold IS this release, there is nothing to offer - whatever the
                # version strings look like.
                "newer": ((bool(asset) and not from_release
                           and payload_engine.newer_than(tag, have)) or app_newer),
            })
        out.sort(key=lambda r: (not r["newer"], str(r["title"] or "").lower()))
        return self._json({"ok": True, "items": out,
                           "update_ready": app_update_state(),
                           "count": sum(1 for r in out if r["newer"])})

    def _payloads_update(self, body):
        """Download an upstream release and put it where the old file was.

        NOTHING IS INSTALLED BY THIS. It replaces the file in the owner's folder, which is what the
        panel then offers to send - so an update and a send stay two decisions, and a download that
        turns out to be wrong has not touched a console.

        The old file is kept as <name>.bak until the new one is proven complete: a truncated
        download that overwrote a working payload would be the worst possible outcome of a button
        labelled "update".
        """
        srv = self.server
        # Something is still being taken, so an armed restart waits for it - see schedule_relaunch.
        defer_relaunch()
        ident = str(body.get("id") or "").strip()
        plat = str(body.get("platform") or "").strip().upper()
        cat, _sig = payload_engine.live_catalog(srv.cfg, WEB_DIR)
        it = None
        for c in (cat.get("items") or []):
            if c.get("id") == ident and str(c.get("platform") or "").upper() == plat:
                it = c
                break
        if not it:
            return self._json({"ok": False, "message": "That is not in the catalogue."}, 404)
        # "This app updates itself, not from here" WAS TRUE AND IS NOT ANY MORE. There is now a
        # release feed for our own artifacts, and this is where a person running the exe at home is
        # meant to take one: the same download, the same .part-then-move, the same marker check
        # inside the bytes before the old file is touched. As for every other item, this replaces
        # the file in the folder and installs nothing - sending it to a console stays a second,
        # separate press.
        repo = it.get("repo")
        if not repo:
            return self._json({"ok": False,
                               "message": "There is no upstream recorded for this one."}, 400)
        rel = payload_engine.github_latest(repo, force=True,
                                           token=payload_engine.gh_token(srv.cfg))
        if rel.get("error"):
            return self._json({"ok": False,
                               "message": "Could not reach GitHub (%s)." % rel["error"]}, 502)
        asset = payload_engine.pick_asset(rel, it)
        if not asset:
            return self._json({"ok": False,
                               "message": "That release has no file for this console."}, 400)
        ok, msg, newver = payload_engine.download_asset(srv.cfg, it, asset,
                                                        log=lambda m: print(m))
        # AND THE APP ITSELF - THE HALF THIS LANE USED TO MISS. Above, the console's ELF is replaced
        # in the folder, which is what the panel then offers to send. Our tile has a second file
        # though, and it is the one running this code: pick_asset() only ever matches .elf, so a PC
        # took the update, reported the new version for the console, and went on being the old
        # companion. Both consoles then read as up to date from a panel that was itself out of date.
        #
        # The owner hit that twice - "I updated it and it still said the old version" - and was
        # right both times. Windows will not let a running .exe be overwritten and WILL let it be
        # renamed, which is the whole method and is why this is safe rather than clever; the
        # reasoning is written out over update_self(). Nothing restarts on its own: the sentence
        # says to reopen the app, because deciding when to close it is the owner's.
        restart = relaunched = False
        if ok and it.get("ours") and payload_engine.running_exe():
            tag = rel.get("tag", "")
            if payload_engine.newer_than(tag, VERSION):
                sok, smsg = payload_engine.update_self(srv.cfg, rel, log=lambda m: print(m))
                restart = sok
                # AND FINISH THE JOB. Replacing the file still left one manual step - close the app
                # and open it again - which is exactly the step an owner does not do, and then
                # reports the update as not having worked. The new build is started here and this
                # one stands down; `relaunched` tells the panel which sentence to show.
                if sok:
                    # ONE PLACE KNOWS THE APP HAS BEEN REPLACED, however it happened - this press or
                    # the background check. Every device that asks is then told the same thing.
                    with _update_lock:
                        _update_ready.update({"version": str(rel.get("tag") or "").lstrip("vV"),
                                              "was": VERSION, "at": int(time.time())})
                    if (srv.cfg.get("updates") or {}).get("auto_restart", False):
                        if schedule_relaunch(srv.cfg["companion"]["port"], log=lambda m: print(m)):
                            relaunched = True
                            smsg = "Updated. The app will restart itself in a moment."
                    else:
                        smsg = ("Version %s is ready. Restart the app to use it."
                                % str(rel.get("tag") or "").lstrip("vV"))
                # BOTH HALVES ARE REPORTED, and a failure here does not undo the ELF that was just
                # fetched - it is on disk, it is good, and it can be sent. Saying "updated" and
                # leaving the old companion running is the one outcome this must never produce.
                msg = ("%s %s" % (msg, smsg)).strip() if smsg else msg
        return self._json({"ok": ok, "message": msg, "version": newver,
                           "restart": restart, "relaunched": relaunched,
                           "tag": rel.get("tag", "")})

    def _payloads_act(self, what, body):
        """Send a payload, keep a homebrew on the console, or install one - at ONE named console."""
        srv = self.server
        b = _bridge_for(srv, self, body)
        if b is None:
            return self._json({"ok": False, "error": "no console configured",
                               "message": "No console is set up yet."}, 400)
        plat = self._pl_platform_of(b)
        it = self._pl_find(str(body.get("id") or "").strip(), body.get("platform") or plat)
        if not it:
            return self._json({"ok": False, "message": "That is not in the catalogue."}, 404)
        # A PLATFORM IS NOT A SUGGESTION. The panel filters, but a caller naming the wrong one gets
        # a sentence rather than a PS5 payload posted at a PS4.
        if str(it.get("platform") or "").upper() != plat:
            return self._json({"ok": False,
                               "message": "That one is for a %s and this console is a %s."
                                          % (it.get("platform"), plat or "?")}, 400)
        title = it.get("title") or it.get("id")

        # THIS PC MAY NOT BE THE ONE HOLDING THE FILE. The console can start a payload it carries
        # itself, which covers every payload on either console; anything else, another companion
        # with the folder can do, and asking it is the honest answer for a machine that does not
        # have the bytes. `via_peer` stops two companions bouncing one request between them.
        if not payload_engine.local_path(srv.cfg, it) and not body.get("via_peer"):
            _needs_bytes = (what in ("seed", "install")
                            or (what in ("send", "run") and it.get("kind") == "payload"
                                and not payload_engine.console_copy_is_current(b, srv.cfg, it)))
            if _needs_bytes:
                try:
                    _reg = getattr(srv, "peers", None)
                    _peers = _reg.known() if _reg is not None else []
                except Exception:
                    _peers = []
                _p = payload_engine.peer_with(_peers, it)
                if _p:
                    print("[payloads] %s is not on this PC - asking %s"
                          % (title, _p.get("name") or _p.get("url")))
                    _r = payload_engine.ask_peer(_p, what, body)
                    if _r is not None:
                        return self._json(_r)

        if what in ("send", "run"):
            if it.get("kind") != "payload":
                return self._json({"ok": False, "message": "That is a homebrew, not a payload."}, 400)
            # A PAYLOAD THAT CHANGES THE JAILBREAK LAYER IS THE OWNER'S CALL, NEVER A SIDE EFFECT.
            # payload_bundle.h states that rule in its own words, after auto-starting one next to
            # kstuff_lite panicked a console. Enforced here, not only in the panel.
            if it.get("layer") == "jailbreak" and not body.get("confirm"):
                return self._json({"ok": False, "needs_confirm": True,
                                   "message": "%s changes the jailbreak layer. Press it again to "
                                              "send it anyway." % title})
            # RUN MEANS USE WHAT IS ALREADY THERE. Both ELFs write the payloads they carry to
            # /data/pkg-mutant-shop/payloads at boot, so starting one costs nothing and copies
            # nothing - which is what "it is already on the console, just activate it" means. Only
            # when the console does not have it does this fall back to sending the bytes, so the
            # ordinary press does the right thing without the owner having to know which case
            # they are in. "send" is the deliberate replace, and never takes the shortcut.
            if what == "run":
                # RUN MEANS "MAKE SURE IT IS THE RIGHT BYTES, THEN START IT", and when this PC has
                # the file that is one path: send_ps5/send_ps4 already skip the copy when the
                # console's is identical, so Run costs nothing extra and can never start a payload
                # the update button has since replaced.
                #
                # Asking the CONSOLE to start its own copy is the fallback for a PC that does not
                # have the file at all. It is not the fast path, because Payload Manager resolves
                # /loadpayload by BASENAME against its own directory - so a file we put anywhere
                # else can silently not be the one that runs, which is a trap this project has
                # already paid for once.
                _here = payload_engine.local_path(srv.cfg, it)
                # THE PS4 HAS NO BASENAME TRAP, so when its copy is already the right bytes it can
                # simply start its own - which saves posting the file over the LAN for nothing. Its
                # loader has no "run the file you already have" verb, but our own ELF does: it reads
                # PB_DIR and posts it to GoldHEN itself.
                if plat == "PS4" and _here and payload_engine.console_copy_is_current(b, srv.cfg, it):
                    if payload_engine.console_load(b, it):
                        return self._json({"ok": True,
                                           "message": "Started %s on the PS4 - it was already "
                                                      "there." % title})
                if not _here:
                    if payload_engine.console_load(b, it):
                        return self._json({"ok": True,
                                           "message": "Started %s from the console's own copy."
                                                      % title})
                    return self._json({"ok": False,
                                       "message": "The console does not have %s, and it is not on "
                                                  "this PC either." % title})

            if plat == "PS4":
                ok, msg = payload_engine.send_ps4(getattr(b, "ip", ""), srv.cfg, it,
                                                  log=lambda m: print(m))
            else:
                ok, msg = payload_engine.send_ps5(b, srv.cfg, it, log=lambda m: print(m))
            return self._json({"ok": ok, "message": msg})

        if what == "seed":
            if it.get("kind") != "homebrew" or it.get("shape") != "pkg":
                return self._json({"ok": False,
                                   "message": "Only a package can be kept on the console."}, 400)
            ok, msg = payload_engine.seed_homebrew(b, srv.cfg, it, log=lambda m: print(m))
            return self._json({"ok": ok, "message": msg})

        if what == "install":
            if it.get("kind") != "homebrew":
                return self._json({"ok": False, "message": "That is a payload, not a homebrew."}, 400)
            # A FOLDER APP IS NOT A PACKAGE, AND IT IS NOT A REFUSAL EITHER. It goes to the
            # drive ShadowMountPlus watches and mounts itself from there - which is exactly what a
            # game stored unpacked already does, so it takes the same lane rather than a new one.
            # _install() routes it on what the registered path IS: a directory goes to the mount
            # lane, a file to the package lane, and nothing here has to tell them apart.
            if it.get("shape") != "pkg" and str(it.get("platform") or "").upper() != "PS5":
                # ShadowMountPlus is a PS5 payload. There is nothing on a PS4 that mounts a folder
                # as a title, so this would copy 260 MB to a console that cannot use it.
                return self._json({"ok": False,
                                   "message": "%s is a folder app, and only the PS5 can mount one."
                                              % title}, 400)
            key = payload_engine.serve_key(it)
            if key not in srv.library.file_registry:
                return self._json({"ok": False, "message": "%s is not on this PC." % title}, 400)
            # THE SAME CALL A GAME MAKES, FIELD FOR FIELD. The page sends install_key, title_id,
            # name, kind, console AND version/content_id/size - the server uses those last three to
            # notice the package is already on the console. Leaving them out does not break the
            # install, it just makes that check blind, and a homebrew is a package like any other.
            # Nothing here looks inside the file: it is handed to the engine the games use and the
            # console's own installer decides.
            # dry_run IS FORWARDED, and it has to be. This route builds its own body for the
            # install lane, and it was not copying the flag across - so asking this endpoint what
            # it WOULD do performed a real install instead. That is the exact trap /api/install
            # grew dry_run for in the first place, reintroduced one layer up.
            return self._install({"install_key": key, "name": title,
                                  "kind": "backup" if it.get("shape") != "pkg" else "base",
                                  # Which drive a folder app lands on. The mount lane resolves
                                  # drive-or-storage-or-ext1 itself; passing it through means the
                                  # panel can offer a picker later without touching this route.
                                  "drive": body.get("drive") or body.get("storage") or "",
                                  "title_id": it.get("title_id") or "",
                                  "content_id": it.get("content_id") or "",
                                  "version": it.get("version") or "",
                                  "size": it.get("size") or 0,
                                  "mode": (body.get("mode") or "now"),
                                  "dry_run": body.get("dry_run"),
                                  "console": body.get("console") or (b.c.get("id") or "")})
        return self._json({"ok": False, "message": "unknown action"}, 400)

    def _dpi_reload(self, body=None):
        """Manual "reload the install engine" — the dock's tag. This route did not exist: the UI
        called it, got 404 "not found", and reported "Reload failed" every single time, while three
        different install-failure messages told the user to press it. It is the same recovery the
        queue runs by itself before a handoff, so it is exposed rather than reimplemented.

        There is nothing to reload: our installer is spawned per install and has already exited by
        the time anyone presses this. What CAN be stale is the hand-over state, so that is what it
        clears - and it says so, rather than reporting a reload that did not happen."""
        srv = self.server
        # `body` IS A PARAMETER, and it has to be, because this is a method - not a branch inside
        # _do_POST where a local `body` happens to be in scope. A mechanical rewrite that added the
        # request body to every _bridge_for call from _do_POST downwards swept this function up
        # too, and Python only notices an undefined name when the line runs: every press of the
        # dock's "clear the install lane" tag and of Settings > Clear answered HTTP 500 with a
        # NameError in pms.log. Nothing was corrupted by it - the queue runs the same cleanup by
        # itself before a hand-off - but a recovery control that always fails is worse than none,
        # because it is pressed exactly when something else has already gone wrong.
        b = _bridge_for(srv, self, body, want="ps5")
        if b is None:
            return self._json({"ok": False, "error": "no console configured"}, 400)

        # This control used to call recover_dpi(), which relaunched host_payload_path() - the
        # etaHEN copy the app shipped - so pressing it resurrected the very daemon the user had
        # removed. There is only one body now and it cannot start anything.
        if True:
            st = b.spawn_status() or {}
            b.spawn_cleanup()
            ready = False
            try:
                ready = bool((b._shop("/api/health", timeout=5) or {}).get("engine_ready"))
            except Exception:
                ready = False
            return self._json({
                "ok": ready, "host": "pms-spawn", "reloaded": False, "cleanup": "spawn-state",
                "detail": ("Our engine is started fresh for every install, so there is nothing to "
                           "reload. Cleared the leftover install state" +
                           (" (one was still marked in flight)" if st.get("busy") else "") +
                           (" - ready for the next install." if ready else
                            " - but Payload Manager is not answering on :8084, so the next install "
                            "cannot start. Reload it on the PS5."))})


    def _manifest(self, title_id):
        srv = self.server
        g = srv.library.find_game(title_id)
        if not g:
            return self._json({"error": "unknown title"}, 404)
        files = []
        for kind in ("base", "updates", "dlc"):
            for it in g.get(kind if kind != "updates" else "updates", []):
                p = srv.library.file_registry.get(it.get("install_key"))
                files.append({"file": it["file"], "kind": it["kind"], "size": it["size"],
                              "version": it.get("version"), "install_key": it.get("install_key"),
                              "sha256": cached_sha(p, srv.hashes) if p else None})
        return self._json({"title_id": title_id, "name": g["name"], "platform": g["platform"],
                           "region": g["region"], "files": files})

    def _space_refusal(self, body, targets):
        """A sentence ONLY when the package cannot fit on any connected drive - otherwise None.

        WHERE A PKG LANDS IS THE CONSOLE'S DECISION. Sony's installer takes it from Installation
        Location (Settings > Storage), which the user sets by hand and which no API reports. An
        earlier version of this tried to infer it from where the console last installed something;
        that inference goes stale the instant the setting is changed, and it refused a perfectly
        good install seconds after the user had switched to Extended Storage on purpose.

        So this does not try to know the destination. It answers only the question that needs no
        inference - "is there anywhere on this console it could possibly go?" - and stays silent for
        everything else. A preflight that guesses is worse than none: it blocks work that would
        have succeeded."""
        need = int(body.get("size") or 0)
        if need <= 0:
            return None
        # WHICH DRIVES COUNT depends on the lane, and this runs before the lane is chosen. A PKG
        # can only ever be installed to internal or extended storage - a USB stick (exFAT) is
        # never a PKG Installation Location - so a 60 GB package with 20 GB free on the M.2 and
        # 200 GB on a stick was let through, and the console then refused it with no bgft row.
        # A backup goes to any drive's homebrew folder, USB included, so it keeps them all.
        reg = self.server.library.file_registry
        key = body.get("install_key")
        local = reg.get(key) if key else None
        pkg_only = bool(local) and local.lower().endswith(".pkg")
        for cid in targets:
            b = self.server.fleet.bridge(cid)
            if not b:
                continue
            try:
                # `ps5` here is the CONSOLE's own answer about ITSELF - a PS4 fills the same key
                # with its own drives - so this is not the fleet-wide legacy key and is correct.
                devs = (b._shop("/api/devices", timeout=8) or {}).get("ps5") or []
            except Exception:
                return None                          # cannot ask -> do not stand in the way
            live = [x for x in devs if x.get("detected") and x.get("free")]
            # THE USB FILTER IS A PS5 RULE AND IT MUST NOT RUN ON A PS4.
            #
            # On a PS5 a stick is exFAT and can never be an Installation Location, so dropping usbN
            # is right. On a PS4 extended storage IS a usbN mount - that is how the PS4 presents the
            # drive most owners install everything to. Dropping them left the internal HDD alone,
            # and an owner whose internal drive is nearly full (the normal arrangement when you
            # have extended storage) was refused instantly, before the console was asked, for a
            # game that had room waiting for it.
            #
            # Worse with two consoles: the loop below returns on the FIRST target that does not
            # fit, so one phantom refusal on the PS4 also cancelled the PS5's half of an "All
            # consoles" install.
            #
            # THE HONEST COST, stated rather than glossed: the PS4 reports a thumb drive and a
            # PS4-formatted extended drive identically - same id shape, same "USB%d" label, no
            # filesystem or role field - so this can now let a package through against a stick it
            # could never install to. That is the right way round for this function, whose own
            # rule is that a preflight which guesses is worse than none: a wrong allow hands the
            # decision back to the console, a wrong refusal is a dead end with no force button.
            # Telling the two apart needs the PS4 payload to say which usbN is extended storage.
            if pkg_only and not b.is_ps4():
                live = [x for x in live if not str(x.get("id") or "").lower().startswith("usb")]
            if not live:
                return None                          # nothing to compare against -> say nothing
            # A little room to work in: the installer writes alongside what it is unpacking.
            roomiest = max(live, key=lambda x: int(x.get("free") or 0))
            if need + (1 << 30) <= int(roomiest["free"]):
                continue                             # it fits somewhere - the console picks where
            # Both halves of the sentence now agree: it fits nowhere, so the way out is space.
            # It used to end "or send it to a drive with room" right after saying no drive had any.
            # NAME THE CONSOLE THAT RAN OUT OF ROOM. It said "the PS5" whichever console this was
            # about - wrong on a PS4 even when the refusal itself is correct, and useless with two
            # consoles connected, where the whole question is which one is full.
            return ("%s needs %s and no drive on the %s has that much free. The most room is %s "
                    "on %s - free some space there, or plug in a drive with room."
                    % (body.get("name") or "This game", human_size(need), b.name,
                       human_size(roomiest.get("free")), roomiest.get("label") or roomiest.get("id")))
        return None

    def _console_for_request(self, all_ids):
        """The console an install means when the caller named none.

        NOT "the first one configured", which is what this used to be. The list is in config order,
        so on a machine with a PS5 and a PS4 an install with no target went to the PS5 - and with
        the PS5 switched off the queue held every job saying "PS5 is not reachable", which reads as
        a broken installer rather than a misaimed one.

        Three signals, cheapest and most certain first:

          1. THE REQUEST CAME FROM A CONSOLE. The page is served by the console it is read on, so
             an install pressed there arrives from that console's own address. Nothing to guess.
          2. There is only one console.
          3. Exactly one console is actually able to install right now. One awake and one asleep is
             not an ambiguous question. This costs one parallel probe of every console, bounded
             by Fleet.status() - and a console whose probe did not finish inside that bound counts
             as neither awake nor asleep, because it did not say.

        Only if none of those settles it does the first entry win, which is the old behaviour and
        the only case where a guess is unavoidable. The queue's "not reachable" message names the
        console it chose, so even the guess is legible."""
        srv = self.server
        if not all_ids:
            return []
        try:
            peer = (self.client_address or ("",))[0] or ""
        except Exception:
            peer = ""
        if peer:
            for cid in all_ids:
                b = srv.fleet.bridge(cid)
                if b is not None and getattr(b, "ip", "") == peer:
                    return [cid]
        if len(all_ids) == 1:
            return list(all_ids[:1])
        # "EXACTLY ONE IS AWAKE" ONLY COUNTS WHEN THE OTHERS ANSWERED. Fleet.status() reports a
        # probe that did not finish as online: None rather than False, and the difference is an
        # install landing on the wrong machine: one live console plus one whose probe merely
        # overran the join used to read as "exactly one awake" and aimed the job at the survivor
        # without a word - the two-console misroute the docstring above was written to prevent. An
        # unknown makes the question ambiguous again, so it falls through to the last-resort guess
        # below, whose choice the queue's message names out loud.
        try:
            _st = srv.fleet.status()
            awake = [c["id"] for c in _st
                     if c.get("online") and c.get("id") in all_ids]
            unsure = [c["id"] for c in _st
                      if c.get("online") is None and c.get("id") in all_ids]
        except Exception:
            awake, unsure = [], []
        if len(awake) == 1 and not unsure:
            return awake
        return list(all_ids[:1])

    def _install(self, body):
        srv = self.server
        url, key = body.get("url"), body.get("install_key")
        target = body.get("console", "")
        all_ids = srv.fleet.ids()
        # NOTHING TO INSTALL ON. With no console address entered yet this fell through to the
        # literal id "ps5-0", which belongs to no console: the press answered ok:true, the UI said
        # "Sent to ... - watch the queue", and the row then failed with "No console configured". The
        # refusal arrived AFTER the success toast instead of in place of it. Answer it here, before
        # a job exists, the same way /api/move and /api/open-ps5 already do.
        if not all_ids:
            return self._json({"ok": False, "error": "no console configured",
                               "message": "No console is set up yet — add its address in Settings, "
                                          "then install."}, 400)
        # The `or ["ps5-0"]` fallback went with it: once all_ids is not empty _console_for_request
        # always answers with at least all_ids[:1], so that fallback could only ever invent an id.
        targets = (all_ids if target == "all"
                   else [target] if target in all_ids
                   else self._console_for_request(all_ids))

        # (Legacy "Send to PS5" FTP + on-console engine install removed — every game now installs
        #  through the single install-host path below: serve over LAN -> hand off the URL.)

        # A patch/DLC/backport can only be applied on top of an installed base game — the console
        # rejects it otherwise. Enforce it here too, not just in the UI, so the rule holds for any
        # caller and the user gets a reason instead of a failed job in the queue.
        kind = (body.get("kind") or "").lower()
        tid = body.get("title_id")

        # A PS5 PACKAGE CANNOT GO ON A PS4, and the console would be the one to say so.
        #
        # They are not the same container at all - a PS5 package begins FIH where a PS4 one
        # begins CNT - so every PS5 game picked with "All consoles" would be handed to the PS4
        # and come back refused, once per game, with the PS4 blamed for it. A PS4 package on a PS5
        # is the opposite case and stays allowed: that console runs them.
        _is_ps5_pkg = bool(tid and str(tid).upper().startswith("PPSA"))
        if not tid and key:
            # NO TITLE ID IS NOT THE SAME AS "NOT A PS5 GAME". An oddly named dump, or a container
            # whose param.sfo could not be read, arrives here with tid empty - and the PPSA test
            # then says nothing at all, so a PS5 package was handed to the PS4 with "All consoles"
            # and came back as a generic console refusal instead of "This is a PS5 game". The
            # library worked out what the file is when it scanned it, so ask it rather than the
            # name. (A backup's platform is "BACKUP", not "PS5"; the mount guard below owns that
            # case, so this leaves it exactly where it was.)
            for _lg in srv.library.games:
                _lits = (_lg.get("base") or []) + (_lg.get("updates") or []) + (_lg.get("dlc") or [])
                if any(str(_it.get("install_key") or "") == key for _it in _lits):
                    _is_ps5_pkg = str(_lg.get("platform") or "").upper() == "PS5"
                    break
        if _is_ps5_pkg:
            # One bridge lookup per candidate. The comprehension this replaces called
            # srv.fleet.bridge(c) twice for every console it considered.
            keep = []
            for c in targets:
                _cb = srv.fleet.bridge(c)
                if _cb is None or not _cb.is_ps4():
                    keep.append(c)
            if not keep:
                return self._json({"ok": False, "error": "This is a PS5 game, and the console it "
                                                         "was sent to is a PS4"}, 400)
            targets = keep

        # A MOUNTED BACKUP CANNOT GO ON A PS4 EITHER, and this one does not announce itself in the
        # title id - a backup of a PS4 game is a CUSA title, so the check above lets it straight
        # through. What made it dangerous is that nothing downstream failed fast: the PS4 implements
        # /api/fs/mkdir and /api/fs/write, so the folder was created and the WHOLE container was
        # uploaded, gigabytes of it, onto a console with no ShadowMount to mount it. Then, with a
        # title id, the mount confirmation timed out after three minutes and told the owner to
        # restart their PS5; without one, the row went green and said the game was ready to play.
        # A wrong answer after a long transfer, or a false success - and the drive filled either way.
        if _is_mount_request(body, srv):
            keep = [c for c in targets
                    if not (srv.fleet.bridge(c) is not None and srv.fleet.bridge(c).is_ps4())]
            if not keep:
                return self._json({"ok": False,
                                   "error": "This is a game backup, which is played in place on a "
                                            "PS5. A PS4 installs games instead - send it a package "
                                            "for this title."}, 400)
            targets = keep

        # DRY RUN: decide, answer, touch nothing.
        #
        # Everything above this line is the TARGETING decision - which console or consoles a
        # request means, and whether the package can go there at all. Everything below it acts on a
        # console. The two-console test suite needs the first and must never cause the second, and
        # it did: it POSTed real installs at whichever consoles were awake, with force:true so that
        # "already installed" could not even short-circuit them, and the owner's PS5 stopped
        # answering twice in one hour immediately after the suite ran. A test that breaks the
        # machine it is run beside is not a test.
        #
        # So the decision is available on its own. No queue row, no hand-over, no console contacted
        # beyond the readiness probes the decision itself needs.
        if body.get("dry_run"):
            return self._json({"ok": True, "dry_run": True, "consoles": targets,
                               "title_id": tid, "kind": kind})

        # An add-on has to go where its base game already lives — installing a patch to a
        # different drive than the game is a guaranteed failure, and the user should not have to
        # remember which drive they picked. Override whatever the drive selector says.
        # Do not reinstall something that is already there. For a base game this is not merely
        # wasteful: a registered title makes the console skip the re-pull entirely and keep the old
        # files, which is the stale_install trap. Pass force:true to override deliberately.
        # WILL IT EVEN FIT? Checked before anything is queued, because the console's own answer
        # for "no" arrives as a bare SCE code after the user has waited - and for the case that
        # actually happens, an 85 GB package onto a drive with 20 GB free, it arrives with no bgft
        # row and a message that blames the file. force:true is still honoured; this only refuses
        # what cannot work.
        # NOT FOR ADD-ONS. A patch or DLC is small and the console has already committed the space
        # for the game it belongs to, so the flat headroom below can refuse one that would have
        # installed - and there is no force button for no_space in the UI, so that is a dead end.
        #
        # ORDER: already-installed FIRST, then space. The installed copy occupies the very space
        # being measured, so an 80 GB game that is already on a drive with 20 GB left was refused
        # as "needs 80 GB and no drive has that much" - a dead end - instead of being skipped with
        # the "already installed - reinstall anyway?" flow the UI has for it.
        if tid and not body.get("force"):
            # A ShadowMount container is checked as a BACKUP: it is registered but has no
            # app.pkg, so the base-game proof could never see it and a repeat Install re-sent
            # the whole container onto the mounted one.
            _lp = srv.library.file_registry.get(key) if key else None
            chk_kind = ("backup" if (_lp and (_lp.lower().endswith(MOUNT_EXTS) or os.path.isdir(_lp)))
                        else (kind or "base"))
            skip_reasons = []
            for cid in targets:
                b = srv.fleet.bridge(cid)
                if not b:
                    continue
                why = b.already_installed(chk_kind, tid,
                                          version=body.get("version") or "",
                                          content_id=body.get("content_id") or "",
                                          pkg_bytes=body.get("size") or 0)
                if why:
                    skip_reasons.append(why)
            if skip_reasons and len(skip_reasons) == len([c for c in targets if srv.fleet.bridge(c)]):
                return self._json({"ok": True, "skipped": True, "ids": [],
                                   "reason": "already_installed", "message": skip_reasons[0]})

        if not body.get("force") and kind not in ("update", "patch", "dlc", "backport"):
            try:
                refusal = self._space_refusal(body, targets)
            except Exception:
                refusal = None
            if refusal:
                return self._json({"ok": False, "error": "no_space", "message": refusal}, 409)

        if kind in ("update", "patch", "dlc", "backport") and tid:
            for cid in targets:
                b = srv.fleet.bridge(cid)
                if not b:
                    continue
                try:
                    for a in (b.console_apps() or []):
                        if a.get("title_id") == tid and a.get("installed_drive"):
                            body["drive"] = drive_id_for_label(a.get("installed_drive"))
                            break
                except Exception:
                    pass
                if body.get("drive"):
                    break
        if kind in ("update", "patch", "dlc", "backport") and tid:
            # THE BASE GAME HAS TO BE ON THE CONSOLE THIS IS BEING SENT TO.
            #
            # This used to build ONE set from every console plus this PC's own installed.json and
            # accept the add-on if the title appeared anywhere in it. With two consoles that is
            # simply the wrong question, and it let a real mistake through: Subnautica's update was
            # sent to the PS4 because CUSA13529 was in the PC's remembered list - from the PS5 - while
            # the PS4 reported its installed titles as CUSA00001, CUSA02365, CUSA14409, CUSA23827,
            # CUSA58072 and PKGM00001. The console accepted the update it had nothing to patch, and
            # the job then sat at 99% for ever.
            #
            # So each target is asked about itself, and the PC's memory is not evidence about a
            # console. A console that cannot be asked is refused rather than guessed at: an add-on
            # installed onto nothing is a wasted transfer at best.
            # ONE CONSOLE LACKING THE BASE IS NOT AN ANSWER FOR THE WHOLE FLEET. This returned
            # on the FIRST target that could not take the add-on, so a patch aimed at several
            # consoles was refused outright and installed nowhere - the console that had the game,
            # and would have taken the patch, was never reached. Drop the ones that cannot take it
            # and keep the rest; the refusal is only the answer when nothing is left, which is
            # exactly what one console produces, so a single-console setup reads as it always did.
            keep, unknown, missing = [], [], []
            for cid in targets:
                b = srv.fleet.bridge(cid)
                if not b:
                    continue
                try:
                    titles = b.installed_titles()
                except Exception:
                    titles = None
                # installed_titles() ANSWERS None WHEN IT COULD NOT ASK - console_apps() returns
                # None on every failure path rather than raising - so `set(... or [])` read a
                # console that is switched off as "has nothing installed" and this refused with
                # base_not_installed, which the page turns into a permanently disabled Install
                # button labelled "Needs base game". A console that is asleep has not said the game
                # is missing; it has said nothing, and base_unknown is the branch for that.
                if titles is None:
                    unknown.append((cid, b.name or cid))
                elif tid in set(titles):
                    keep.append(cid)
                else:
                    missing.append((cid, b.name or cid))
            if keep:
                targets = keep
            elif unknown:
                return self._json({"ok": False, "error": "base_unknown", "title_id": tid,
                                   "console": unknown[0][0],
                                   "message": "%s did not answer, so there is no way to tell "
                                              "whether the game this %s belongs to is installed "
                                              "there. Turn the console on and try again."
                                              % (unknown[0][1], kind)}, 409)
            elif missing:
                return self._json({"ok": False, "error": "base_not_installed", "title_id": tid,
                                   "console": missing[0][0],
                                   "message": "Install the game on %s first — a %s can only be "
                                              "applied on top of the installed game."
                                              % (missing[0][1], kind)}, 409)

        # --- MOUNT lane: a ShadowMount backup (.ffpfsc etc.) -> FTP to the scan folder, not DPI ---
        # A registered path that is a DIRECTORY is a game stored unpacked — it mounts exactly like a
        # container, so it belongs in this lane too rather than falling through to the PKG installer.
        if key and key in srv.library.file_registry and (
                srv.library.file_registry[key].lower().endswith(MOUNT_EXTS)
                or os.path.isdir(srv.library.file_registry[key])):
            path = srv.library.file_registry[key]
            # ONE drive value for both the destination and the row. dest used to follow
            # drive-or-storage while the row recorded drive-or-"ext1", so a caller that sent
            # only `storage` (or neither) got a row saying ext1 over a file going elsewhere.
            # Same resolution the two peer lanes below have always used.
            _drive = body.get("drive") or body.get("storage") or "ext1"
            dest = mount_dest_for_drive(srv.cfg, _drive)
            jobs = [srv.queue.add({"name": body.get("name", os.path.basename(path)),
                                   "title_id": body.get("title_id"), "kind": "backup", "lane": "mount",
                                   "console": cid, "drive": _drive,
                                   "local_path": path, "dest": dest,
                                   # THE CONSOLE PULLS IT. _run_mount has always preferred that -
                                   # it builds http://<this PC>/library/<key> and hands it to the
                                   # console's own downloader - but it decides from `url` or `key`,
                                   # and this job set NEITHER. So a backup that lives on the very PC
                                   # you press Install from took the push instead, and the pull lane
                                   # only ever ran for a game on ANOTHER PC. That is the whole of
                                   # "transfers are slow": measured off this console's own log,
                                   # pushed 16.9-18.5 MB/s against 71-112 MB/s pulled, 6x, and
                                   # Marvel's Wolverine at 112 GB is the difference between 1h50m
                                   # and about 17 minutes. The push stays below as the fallback for
                                   # a folder-shaped game and for a console too old to be asked.
                                   "key": key,
                                   "hold": body.get("mode") == "queued",   # + Queue must not auto-start
                                   "total": srv.library.file_sizes.get(key, 0)}) for cid in targets]
            return self._json({"ok": True, "ids": jobs, "lane": "mount", "dest": dest, "consoles": targets})

        # --- CONSOLE-LOCAL lane: the package is already ON the PS5 (USB stick, external
        #     drive, internal storage). There is nothing to serve or transfer, so hand the
        #     path to the console app, which installs it with the same call it uses when the
        #     PC is switched off. Without this the key was simply not in the PC file registry
        #     and the request came back as "unknown install_key".
        if key and str(key).startswith("local:"):
            # The package is already ON the console, so nothing is transferred - but it still
            # goes through the QUEUE like every other install, so "+ Queue" holds it instead of
            # starting it immediately and the job is visible with the rest.
            local_path = str(key)[len("local:"):]
            # WHICH CONSOLE'S DRIVE IS THIS? A "local:" key is a PATH AND NOTHING ELSE, and both
            # consoles expose /mnt/usbN, so a stick plugged into the PS5 could be queued against
            # the PS4 - which relayed its own "That package is not on the console any more", a
            # sentence that sends the owner to re-check a stick that is perfectly fine, in the
            # other machine. With console:"all" it was worse: one press produced a real install on
            # one console and that bogus error row on the other. So ask each console whether the
            # package is one of its own and keep only the ones that said yes.
            #
            # Skipped entirely with a single console, where there is only one drive it can be in:
            # a one-console setup does no extra work and behaves exactly as before. The lookup
            # itself never contacts a console - see console_usb_packages_cached().
            if len(all_ids) > 1:
                owners = []
                for _oc in all_ids:
                    _ob = srv.fleet.bridge(_oc)
                    _ogs = _ob.console_usb_packages_cached() if _ob is not None else None
                    for _og in (_ogs or []):
                        _ois = ((_og.get("base") or []) + (_og.get("updates") or [])
                                + (_og.get("dlc") or []))
                        if any(str(_it.get("install_key") or "") == key for _it in _ois):
                            owners.append(_oc)
                            break
                # ONLY ACT ON EVIDENCE. No owner found means no console has listed its drives in
                # this process - not that the package is nowhere - and refusing on that would block
                # an install that works today.
                if owners:
                    _keep = [c for c in targets if c in owners]
                    if not _keep:
                        _ob = srv.fleet.bridge(owners[0])
                        _on = (_ob.name if _ob is not None else None) or owners[0]
                        return self._json({"ok": False, "error": "wrong_console",
                                           "console": owners[0],
                                           "title_id": body.get("title_id"),
                                           "message": "That package is on a drive plugged into %s, "
                                                      "so only %s can install it." % (_on, _on)},
                                          409)
                    targets = _keep
            if not (srv.fleet.bridge(targets[0]) if targets else _bridge_for(srv, self, body, want="ps5")):
                return self._json({"ok": False, "error": "no console configured"}, 400)
            jobs = [srv.queue.add({"name": body.get("name") or os.path.basename(local_path),
                                   "title_id": body.get("title_id"),
                                   "kind": body.get("kind") or "base", "lane": "console-local",
                                   "console": cid, "drive": body.get("drive") or "",
                                   "local_path": local_path,
                                   "hold": body.get("mode") == "queued",
                                   "total": body.get("size") or 0}) for cid in targets]
            return self._json({"ok": True, "ids": jobs, "lane": "console-local",
                               "dest": "console", "consoles": targets,
                               "queued": body.get("mode") == "queued"})

        # --- MULTI-PART: one release split across several packages ---
        # Installing only part 1 leaves the title broken, so every piece is queued, in order,
        # and the queue installs them one at a time per console. Works the same whichever
        # lane they end up in, because expansion happens before the lane is chosen.
        if key and body.get("_part_of") is None:
            parts_item = None
            for cand in srv.library.games:
                for it in (cand.get("base") or []) + (cand.get("updates") or []) + (cand.get("dlc") or []):
                    if it.get("install_key") == key and (it.get("parts") or []):
                        parts_item = it
                        break
                if parts_item:
                    break
            if parts_item and len(parts_item["parts"]) > 1:
                if parts_item.get("parts_gap"):
                    return self._json({"ok": False, "error": "missing_part",
                                       "message": "This release is split into parts and one is "
                                                  "missing, so installing it would leave the game "
                                                  "broken. Add every part and try again."}, 409)
                ids, failed, skipped = [], None, 0
                self._json_muted = True                 # collect, do not write - see _json()
                try:
                    for p in parts_item["parts"]:
                        sub = dict(body)
                        sub["install_key"] = p["install_key"]
                        sub["_part_of"] = key
                        sub["name"] = "%s (part %d/%d)" % (
                            body.get("name") or parts_item.get("name") or "Game",
                            p["part"], len(parts_item["parts"]))
                        # Every part takes the SAME mode as the request. Parts 2..n used to be
                        # forced to "queued" - which means HELD, "press Start" - while this reply
                        # promised they "install one after the other"; nothing installed them
                        # after part 1 and the UI covered it with a toast. Ordering is the
                        # queue's own: parts are added in order and _claim() runs one job per
                        # console at a time, so the parts never overlap - unless per-console
                        # parallelism was switched on, in which case the old hold stays.
                        if ids and srv.queue.allow_ppc:
                            sub["mode"] = "queued"
                        res = self._install(sub)
                        payload, code = res if isinstance(res, tuple) else (res, 200)
                        # Honour what each part actually said. This was thrown away: a part that
                        # was refused, or already installed, was counted as queued anyway.
                        if isinstance(payload, dict) and payload.get("skipped"):
                            skipped += 1
                        elif isinstance(payload, dict) and payload.get("ok"):
                            ids.extend(payload.get("ids") or [])
                        elif failed is None:
                            failed = (payload, code, p.get("part"))
                finally:
                    self._json_muted = False
                if failed is not None:
                    payload, code, partno = failed
                    msg = (payload or {}).get("message") or (payload or {}).get("error") or "refused"
                    return self._json({"ok": False, "multi_part": True,
                                       "error": (payload or {}).get("error") or "part_failed",
                                       "part": partno, "ids": ids,
                                       "message": "Part %s could not be queued (%s). The parts that "
                                                  "were queued are in the queue; a release is only "
                                                  "playable with all of them."
                                                  % (partno, msg)}, code if code >= 400 else 409)
                total_parts = len(parts_item["parts"])
                if skipped == total_parts:
                    return self._json({"ok": True, "skipped": True, "multi_part": True, "ids": [],
                                       "reason": "already_installed",
                                       "message": "Every part of this release is already installed."})
                return self._json({"ok": True, "multi_part": True, "ids": ids,
                                   "parts": total_parts, "skipped_parts": skipped,
                                   "message": "Queued %d part%s — they install one after the other."
                                              % (len(ids), "" if len(ids) == 1 else "s")})

        total, source, src = 0, "external", {"name": "external"}
        # --- PEER lane: the game lives on ANOTHER PC on this network ---
        # The console fetches it straight from that PC, so nothing is proxied through here and
        # the transfer does not depend on this machine staying awake. build_federated_library()
        # put the peer's own LAN url on the item; without this branch the key was simply not in
        # our file registry and the install came back as "unknown install_key".
        if not url and key and key not in srv.library.file_registry:
            g = None
            peer_base = ""
            peer_item = None
            for cand in build_federated_library(srv).get("games", []):
                for it in (cand.get("base") or []) + (cand.get("updates") or []) + (cand.get("dlc") or []):
                    if it.get("install_key") == key and it.get("peer_url"):
                        g, url, source = cand, it["peer_url"], (cand.get("source_pc") or "peer")
                        total = it.get("size") or 0
                        peer_base = it["peer_url"]
                        peer_item = it
                        break
                if url:
                    break
            # A backup does not go through the PKG installer — it has to reach the console's
            # ShadowMount folder. Locally that already works; from another PC the file simply was
            # not here yet, which is the only piece that was missing. Pull it into this library,
            # then hand it to the same mount lane a local backup uses. Both halves are the existing,
            # proven ones — this only runs them in order.
            # A backup has to reach the console's ShadowMount folder, and the PC that HOLDS it can
            # send it there itself — one transfer instead of two. Pulling it here first and pushing
            # it on afterwards moves a 100GB container across the network twice and, worse, looks
            # like the app is copying between PCs when the user asked for it on the PS5.
            # Ask the owner to run its own (already working) mount lane, and mirror its progress.
            if url and peer_item is not None and is_backup_item(peer_item, g) and peer_base:
                # Queue it HERE and nowhere else. The other PC is only asked to move the file, and
                # only at the moment this task actually runs — see _run_peer_delegate. Contacting it
                # at queue time and passing this queue's mode along was wrong: a game added with
                # "+ Queue" sat held on the other machine, so pressing Start here did nothing and the
                # app told you to go and start it over there. Which device you press the button on is
                # not something you should ever have to think about.
                # Queued as a MOUNT task carrying BOTH routes. _run_mount asks the console to
                # fetch `url` itself - one hop, straight from the PC that holds it, with nothing of
                # ours running there - and falls back to delegating through owner_url/owner_key if
                # this console's ELF is too old to have /api/engine/fetch. The decision is made when
                # the task RUNS, not now, because which console it lands on is not known here and a
                # queued job may not run for hours.
                _drive = body.get("drive") or body.get("storage") or "ext1"
                jobs = [srv.queue.add({
                    "name": body.get("name") or g.get("name") or peer_item.get("file"),
                    "title_id": g.get("title_id"), "kind": "backup", "lane": "mount",
                    "console": cid, "drive": _drive,
                    "dest": mount_dest_for_drive(srv.cfg, _drive),
                    "url": peer_base,                       # the console pulls from here
                    "remote_name": os.path.basename(unquote(peer_item.get("install_key") or "")),
                    "from_pc": g.get("source_pc"),
                    "owner_url": peer_base.split("/library/")[0],
                    "owner_key": peer_item.get("install_key"),
                    "hold": body.get("mode") == "queued",   # this queue's hold, honoured locally
                    "total": total}) for cid in targets]
                return self._json({"ok": True, "ids": jobs, "lane": "mount",
                                   "via": g.get("source_pc"), "pull": True,
                                   "drive": _drive})
            if url and peer_item is not None and is_backup_item(peer_item, g):
                plat = (g.get("platform") or "PS4").upper()
                root = srv.cfg.get("library", {}).get("root") or LIBRARY_ROOT
                dest_dir = os.path.join(root, plat if plat in LIBRARY_LAYOUT else "")
                drive = body.get("drive") or body.get("storage") or "ext1"
                jobs = [srv.queue.add({
                    "name": body.get("name") or g.get("name") or peer_item.get("file"),
                    "title_id": g.get("title_id"), "kind": "backup", "lane": "peer-mount",
                    "console": cid, "drive": drive,
                    "dest": dest_dir, "url": url,
                    "file": peer_item.get("file") or os.path.basename(url),
                    "from_pc": g.get("source_pc"),
                    "mount_dest": mount_dest_for_drive(srv.cfg, drive),
                    "hold": body.get("mode") == "queued",
                    "total": total}) for cid in targets]
                return self._json({"ok": True, "ids": jobs, "lane": "peer-mount",
                                   "from": g.get("source_pc"), "drive": drive,
                                   "dest": mount_dest_for_drive(srv.cfg, drive)})
            if url:
                # base_url lets served_url() derive the peer's /api/served/<key>, which is how
                # this lane gets real progress instead of sitting at 0%.
                root = peer_base.split("/library/")[0] if "/library/" in peer_base else ""
                src = {"name": source, "peer": True, "kind": "companion",
                       "base_url": (root + "/library") if root else ""}

        # --- INSTALL lane: PKG via DPI v2 ---
        if not url and key:
            if key not in srv.library.file_registry:
                return self._json({"error": "unknown install_key"}, 400)
            total = srv.library.file_sizes.get(key, 0)
            filename = os.path.basename(srv.library.file_registry[key])
            _tb = srv.fleet.bridge(targets[0]) if targets else None      # [B11] PC IP on the console's subnet
            lan_url = "http://%s:%d/library/%s" % (companion_ip_for(_tb.ip if _tb else None),
                                                   srv.cfg["companion"]["port"], key)
            resolved, src_name, _rank = srv.engine.resolve(filename)   # best reachable mirror, LAN fallback
            url, source = (resolved or lan_url), (src_name or "companion-lan")
            src = next((s for s in srv.engine.sources if s.get("name") == source), {"name": "companion-lan"})
        if not url:
            return self._json({"error": "need url or install_key"}, 400)
        local_progress = source == "companion-lan"     # local byte-count vs remote /api/served poll
        progress_url = None
        if not local_progress and key and (source_engine.is_companion(src) or src.get("peer")):
            progress_url = source_engine.served_url(src, key)
        measurable = local_progress or bool(progress_url)
        hold = body.get("mode") == "queued"                             # [B8] add-to-queue vs install-now
        # The PS4 needs the package described when the task is handed over, and `total` above is
        # deliberately 0 for a job whose progress cannot be measured - so the real size travels in
        # its own field. Whatever the caller told us wins; the library fills in the rest.
        _facts = pkg_facts_for_key(getattr(srv, "library", None), key)
        _cid = str(body.get("content_id") or _facts["content_id"] or "")
        try:
            _psize = int(body.get("size") or 0) or _facts["size"] or int(total or 0)
        except (TypeError, ValueError):
            _psize = _facts["size"] or int(total or 0)
        jobs = [srv.queue.add({"name": body.get("name", "PKG"), "title_id": body.get("title_id"),
                               "content_id": _cid, "pkg_size": _psize,
                               "kind": body.get("kind", "base"), "lane": "install", "hold": hold,
                               "drive": body.get("drive", "internal"), "url": url, "console": cid, "source": source,
                               "key": key if measurable else None, "total": total if measurable else 0,
                               # Remember that this URL points at US, so it can be rebuilt at
                               # submit time. Held overnight, the PC can move (DHCP, or Windows
                               # switching Ethernet to Wi-Fi) and every queued job would then hand
                               # etaHEN a URL on an address that no longer exists.
                               # Carried so the duplicate-submission guard can be overridden
                               # deliberately. `force` already bypasses the already-installed check
                               # above; it has to mean the same thing all the way down.
                               "force": bool(body.get("force")),
                               "relan": bool(local_progress and key),
                               "local_progress": local_progress, "progress_url": progress_url}) for cid in targets]
        return self._json({"ok": True, "ids": jobs, "url": url, "source": source, "consoles": targets})

    def _static_resolve(self, rel):
        """(full_path, None) for a file under WEB_DIR, or (None, (status, text)) for the refusal
        GET would send. One resolver, so HEAD and GET cannot disagree about what exists."""
        rel = rel.split("?")[0]
        safe = os.path.normpath(rel).replace("\\", "/").lstrip("/")
        if ".." in safe.split("/"):
            return None, (400, "bad path")
        # A ".." check alone is NOT enough on Windows: os.path.join(WEB_DIR, "C:/Windows/win.ini")
        # throws WEB_DIR away entirely, because the drive letter makes the second argument absolute.
        # This handler is the catch-all for every unmatched GET, and the companion listens on
        # 0.0.0.0, so that turned "GET /C:/Windows/win.ini" into an arbitrary file read for anything
        # on the LAN (verified: HTTP 200 with the file body). Resolve the real path and require it
        # to be inside WEB_DIR - that covers drive letters, UNC paths and symlinks in one check.
        full = os.path.realpath(os.path.join(WEB_DIR, safe))
        try:
            if os.path.commonpath([full, os.path.realpath(WEB_DIR)]) != os.path.realpath(WEB_DIR):
                return None, (400, "bad path")
        except ValueError:                      # different drive entirely - never ours
            return None, (400, "bad path")
        if not os.path.isfile(full):
            if rel in ("", "index.html"):
                return None, (500, "web/index.html missing")
            return None, (404, "not found")
        return full, None

    # Compressible static types. Images are already compressed and are never gzipped.
    _GZIP_TYPES = ("text/html", "text/css", "application/javascript", "text/plain")

    def _static(self, rel):
        """A file from web/. Gzipped for the types that shrink, validated with an ETag, and told
        how long it may be kept.

        index.html is 636 KB and this served it raw on every page load, with no validator and no
        cache policy - the largest response the server has was the one left uncompressed, while
        _json() gzips a 200 KB library and _art_response() sends immutable covers. Now:
          * ETag from size+mtime, 304 on a matching If-None-Match - a reload costs ~150 bytes;
          * gzip when the client accepts it (index.html: ~636 KB -> ~150 KB), cached in memory
            per (path, size, mtime) so the compression is paid once per file version;
          * Cache-Control no-cache for the page and config.js (always revalidate, so a new build
            is picked up at once) and a week for assets/* (logos and icons, which carry no
            version and never change between releases in a way that matters).
        Content-Length is always the length of the bytes actually sent."""
        full, err = self._static_resolve(rel)
        if full is None:
            return self._plain(err[1], err[0])
        # STAT, THEN DECIDE, THEN READ. This read the whole file before working out that it only
        # had to send a 304 - 811 KB off disk per reload, for a ~150-byte answer, on the thread the
        # browser is waiting on. Nothing in the decision needs the bytes: the ETag is size+mtime and
        # the gzip test only wanted a length, which stat already has.
        try:
            st = os.stat(full)
        except OSError:
            return self._plain("read error", 500)
        ctype = guess_type(full)
        safe = os.path.normpath(rel.split("?")[0]).replace("\\", "/").lstrip("/")
        cache_ctl = ("public, max-age=604800" if safe.split("/")[0] == "assets"
                     else "no-cache")
        enc = None
        if (ctype.split(";")[0] in self._GZIP_TYPES and st.st_size > 1024
                and "gzip" in (self.headers.get("Accept-Encoding") or "").lower()):
            enc = "gzip"
        etag = '"%x-%x%s"' % (st.st_size, int(st.st_mtime), "-gz" if enc else "")
        if etag_matches(self.headers.get("If-None-Match"), etag):
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", cache_ctl)
            if enc:
                self.send_header("Vary", "Accept-Encoding")
            self._cors()
            self.end_headers()
            return
        try:
            with open(full, "rb") as f:
                data = f.read()
        except OSError:
            return self._plain("read error", 500)
        if enc:
            ck = (full, st.st_size, int(st.st_mtime))
            with _STATIC_LOCK:
                ent = _STATIC_GZ.get(full)
            if ent and ent[0] == ck:
                data = ent[1]
            else:
                try:
                    gz = gzip.compress(data, 6)
                    with _STATIC_LOCK:
                        _STATIC_GZ[full] = (ck, gz)
                    data = gz
                except Exception:
                    enc = None                  # never fail a page over compression
                    etag = '"%x-%x"' % (st.st_size, int(st.st_mtime))
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        if enc:
            self.send_header("Content-Encoding", enc)
            self.send_header("Vary", "Accept-Encoding")
        self.send_header("ETag", etag)
        self.send_header("Cache-Control", cache_ctl)
        self.send_header("Content-Length", str(len(data)))
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    THUMB_PX = 320          # ~1.5x the largest card so it stays crisp on a 4K TV

    def _thumb_for(self, tid):
        """Path to a card-sized cover for `tid`, generating it once. "" if we have no source.

        Never raises and never blocks the caller twice: on any failure it returns "" and the caller
        falls back to the full-size icon, so a missing codec or an odd PNG degrades to today's
        behaviour rather than a broken card.
        """
        if not tid or not re.match(r"^[A-Za-z0-9_-]{1,32}$", tid):
            return ""
        try:
            os.makedirs(THUMB_DIR, exist_ok=True)
        except OSError:
            return ""
        out = os.path.join(THUMB_DIR, tid + ".webp")
        srcp = os.path.join(ICON_DIR, tid + ".png")
        if os.path.isfile(out):
            # regenerate only if the source is newer than the thumb
            try:
                if not os.path.isfile(srcp) or os.path.getmtime(out) >= os.path.getmtime(srcp):
                    return out
            except OSError:
                return out
        if not os.path.isfile(srcp):
            # EITHER CONSOLE MIGHT HAVE IT. A PS4 game's artwork is on the PS4 and a PS5 game's on
            # the PS5, so asking only the first console meant half the library drew initials on a
            # machine that owns both. Ask each in turn and stop at the one that answers.
            b = _bridge_for(self.server, self)
            if b:
                try:
                    b.console_icon(tid)
                except Exception:
                    pass
        if not os.path.isfile(srcp):
            return ""
        try:
            from PIL import Image
            im = Image.open(srcp)
            im.load()
            if im.mode not in ("RGB", "RGBA"):
                im = im.convert("RGBA")
            im.thumbnail((self.THUMB_PX, self.THUMB_PX), Image.LANCZOS)
            tmp = out + ".part"                     # never let a reader see a half-written file
            im.save(tmp, "WEBP", quality=82, method=4)
            os.replace(tmp, out)
            return out
        except Exception as e:
            print("[thumb] %s: %r" % (tid, e))
            return ""

    def _art_response(self, path, data, ctype):
        """Serve cover art with a validator so a reload costs ~150 bytes instead of the file.

        Art for a title id never changes, so it is `immutable`; the ETag is still derived from
        size+mtime so a regenerated thumbnail is picked up immediately. Without this every icon
        was re-fetched in full once a day - up to 541,883 bytes each.
        """
        try:
            st = os.stat(path)
            etag = '"%x-%x"' % (st.st_size, int(st.st_mtime))
        except OSError:
            etag = ""
        if etag and (self.headers.get("If-None-Match") or "").strip() == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "public, max-age=604800, immutable")
            self._cors()
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "public, max-age=604800, immutable")
        if etag:
            self.send_header("ETag", etag)
        self._cors()
        self.end_headers()
        self.wfile.write(data)

    def _serve_thumb(self, name):
        base = os.path.basename(name.split("?")[0])
        tid = re.sub(r"\.(webp|png|jpg)$", "", base, flags=re.I)
        path = self._thumb_for(tid)
        if not path:
            return self._serve_icon(tid + ".png")   # graceful: full size beats no cover
        try:
            with open(path, "rb") as f:
                data = f.read()
        except OSError:
            return self._plain("no thumb", 404)
        return self._art_response(path, data, "image/webp")

    def _serve_icon(self, name):
        base = os.path.basename(name.split("?")[0])
        full = os.path.join(ICON_DIR, base)
        if not os.path.isfile(full):
            # lazy-fetch the real icon from the console (base is "<title_id>.png")
            tid = base[:-4] if base.lower().endswith(".png") else base
            # Try every console, not just the first: the icon is on whichever one has the game.
            for _cid in (self.server.fleet.ids() or []):
                b = self.server.fleet.bridge(_cid)
                if b is None:
                    continue
                try:
                    b.console_icon(tid)
                except Exception:
                    continue
                if os.path.isfile(full):
                    break
            b = _bridge_for(self.server, self)
            if b:
                b.console_icon(tid)
        if not os.path.isfile(full):
            return self._plain("no icon", 404)
        with open(full, "rb") as f:
            data = f.read()
        return self._art_response(full, data, "image/png")

    @staticmethod
    def _file_validators(path):
        """ETag + Last-Modified for a served library file, as header pairs. Additive: nothing
        that reads /library/ needs them, but _run_pc_copy's Range resume can now tell that the
        file it is appending to is still the one it started on."""
        try:
            st = os.stat(path)
        except OSError:
            return []
        return [("ETag", '"%x-%x"' % (st.st_size, int(st.st_mtime))),
                ("Last-Modified", time.strftime("%a, %d %b %Y %H:%M:%S GMT", time.gmtime(st.st_mtime)))]

    @staticmethod
    def _content_disposition(path):
        """A Content-Disposition the wire can carry for ANY file name.

        HTTP headers are sent latin-1. A name outside that set - six live backups here carry
        characters that are not - raised UnicodeEncodeError inside send_header, so the console's
        GET died before a single byte was sent and the install failed with no answer at all.
        RFC 6266: an ASCII `filename` for old readers, plus `filename*` with the real name
        percent-encoded as UTF-8 for everything else. Nothing reads this header to decide
        anything - the console names the file from the URL - so the fallback is cosmetic."""
        fn = os.path.basename(path)
        plain = fn.encode("ascii", "ignore").decode("ascii").replace('"', "").replace("\\", "")
        cd = 'attachment; filename="%s"' % (plain.strip() or "package")
        if plain != fn:
            cd += "; filename*=UTF-8''" + quote(fn, safe="")
        return cd

    def _serve_library(self, key):
        srv = self.server
        raw = key.split("?")[0]
        rk = registry_key(srv.library.file_registry, raw)
        if not rk or not os.path.isfile(srv.library.file_registry[rk]):
            return self._plain("unknown library file", 404)
        path = srv.library.file_registry[rk]
        # The 300 s `timeout` on this class is a reaper for connections that never send a
        # request line. It must not arm the socket a PKG streams over: a wfile.write that
        # blocks longer than that (BGFT paused mid-chunk, a receive window that stays full)
        # would raise socket.timeout and drop the stream. Back to the stdlib default here.
        try:
            self.connection.settimeout(None)
        except Exception:
            pass
        size = os.path.getsize(path)
        start, end, code = 0, size - 1, 200
        rng = self.headers.get("Range")
        if rng:
            m = re.match(r"bytes=(\d*)-(\d*)", rng)
            if m:
                g1, g2 = m.group(1), m.group(2)
                if g1 == "" and g2:                 # suffix range: last N bytes
                    start, end = max(0, size - int(g2)), size - 1
                else:
                    if g1:
                        start = int(g1)
                    if g2:
                        end = min(int(g2), size - 1)
                if start > end:
                    self.send_response(416)
                    self.send_header("Content-Range", "bytes */%d" % size)
                    # A 416 with no Content-Length left a client that waits for a body waiting;
                    # the browser fetch() that hits it needs the CORS header to even read it.
                    self.send_header("Content-Length", "0")
                    self._cors()
                    self.end_headers()
                    return
                code = 206
        length = end - start + 1
        self.send_response(code)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if code == 206:
            self.send_header("Content-Range", "bytes %d-%d/%d" % (start, end, size))
        self.send_header("Content-Disposition", self._content_disposition(path))
        for k, v in self._file_validators(path):
            self.send_header(k, v)
        self.end_headers()
        # Two buckets: one per requesting console (what our own jobs read), and the shared
        # aggregate (what a peer companion polls, since it only knows the key).
        peer_ip = ""
        try:
            peer_ip = self.client_address[0]
        except Exception:
            peer_ip = ""
        agg = srv.transfers.setdefault(rk, {"max": 0, "total": size})
        # A FINISHED SERVE IS NOT THIS SERVE'S PROGRESS. This is the by-key aggregate, and it is the
        # only bucket a PEER can poll, so installing the same package from the same peer PC a second
        # time read that peer's OLD high-water mark on the very first poll: the row flashed
        # "Downloading 90%", the `mx >= tot` test broke straight out of the transfer loop, and the
        # 90-second stall detector under it therefore never ran even once - so a peer transfer that
        # died reported nothing at all until the 2100 s promote deadline expired, and then blamed
        # something that was not the cause. A request that starts at offset 0 against a bucket that
        # has already reached its total is a NEW transfer, so the counter starts again. Both halves
        # of that test matter: a resume carries a Range header (start > 0) and a transfer still in
        # flight has max < total, so neither can be rewound under a console that is pulling.
        if start == 0 and agg.get("max", 0) and agg["max"] >= (agg.get("total") or 0):
            agg["max"] = 0
        agg["total"] = size
        tr = srv.transfers.setdefault("%s|%s" % (rk, peer_ip), {"max": 0, "total": size}) \
            if peer_ip else agg
        tr["total"] = size
        chunk = max(65536, int(srv.cfg.get("performance", {}).get("serve_chunk_bytes", 1048576)))   # [B4] tunable serve buffer
        with open(path, "rb") as f:
            f.seek(start)
            remaining, pos = length, start
            while remaining > 0:
                buf = f.read(min(chunk, remaining))
                if not buf:
                    break
                try:
                    self.wfile.write(buf)
                except (BrokenPipeError, ConnectionResetError):
                    return
                pos += len(buf)
                remaining -= len(buf)
                if pos > agg["max"]:
                    agg["max"] = pos          # aggregate, for peers polling by key alone
                if pos > tr["max"]:
                    tr["max"] = pos


def etag_matches(header, etag):
    """Does the client's If-None-Match cover the tag we hold?

    A SUBSTRING search over the header, not an equality test - the same rule the two payloads use
    (ps5-app/onconsole/server.c, ps4-app/onconsole/server_ps4.c), so all three servers answer a
    reload the same way. Equality is wrong twice over: the value may be a LIST of tags, and any
    cache between here and the browser is allowed to hand back a WEAKENED form of a tag it
    revalidates (W/"..."). Either one misses, and a validator that never matches is invisible - it
    just quietly puts the whole file back on the wire. "*" means whatever the server holds.
    """
    if not header or not etag:
        return False
    h = header.strip()
    if "*" in h:
        return True
    return etag in h


# Gzipped copies of static files, keyed by path -> ((path, size, mtime), bytes). See _static().
_STATIC_GZ = {}
_STATIC_LOCK = threading.Lock()


class CompanionServer(ThreadingHTTPServer):
    """ThreadingHTTPServer with the two settings a LAN app with several clients needs.

    The stdlib backlog is 5; a page load fires health + library + queue + storage + network and
    a burst of thumbnails from three devices at once, and with one connection per request that
    overflowed it - Windows answers the overflow with a reset, seen as a broken card image or a
    poll that fails with no retry. daemon_threads is already the parent's default."""
    request_queue_size = 64

    def handle_error(self, request, client_address):
        """An exception that escaped a handler thread. The parent prints a traceback to stderr,
        which the frozen exe does not have - so it vanished. One line into pms.log instead."""
        _et, ev, _tb = sys.exc_info()
        who = client_address[0] if client_address else "?"
        if isinstance(ev, (BrokenPipeError, ConnectionResetError, ConnectionAbortedError,
                           socket.timeout, TimeoutError)):
            print("[http] %s dropped the connection (%s)" % (who, type(ev).__name__))
            return
        print("[http] unhandled error serving %s: %s" % (who, _exc_line(ev) if ev else "?"))


def guess_type(path):
    p = path.lower()
    return ("text/html; charset=utf-8" if p.endswith(".html") else
            "application/javascript" if p.endswith(".js") else
            "text/css" if p.endswith(".css") else
            "application/json" if p.endswith(".json") else
            "image/png" if p.endswith(".png") else
            "image/svg+xml" if p.endswith(".svg") else "application/octet-stream")


# --------------------------------------------------------------------------- #
# main                                                                          #
# --------------------------------------------------------------------------- #
# ---- PS5 -> PC log channel -------------------------------------------------
# On-console payloads (e.g. the tile installer) open a TCP connection to this
# listener and send one diagnostic line at a time, so we can debug them from the
# PC without watching the TV. Read them via  GET /api/ps5-log.
PS5_LOG = []
PS5_LOG_LOCK = threading.Lock()
PS5_LOG_MAX = 1000


def ps5_log_add(line, src=""):
    ts = time.strftime("%H:%M:%S")
    with PS5_LOG_LOCK:
        PS5_LOG.append({"t": ts, "src": src, "line": line})
        if len(PS5_LOG) > PS5_LOG_MAX:
            del PS5_LOG[:len(PS5_LOG) - PS5_LOG_MAX]
    print("[PS5 %s] %s" % (ts, line))


PS5_LOG_LINE_MAX = 8192          # one line; a sender that never sends a newline is cut here
PS5_LOG_CONN_MAX = 2000          # lines per connection; every line is also a pms.log print()


def _ps5_log_conn(conn, addr):
    ip = addr[0] if addr else "?"
    buf = b""
    n = 0
    conn.settimeout(15)
    try:
        while True:
            data = conn.recv(1024)
            if not data:
                break
            buf += data
            # A peer streaming bytes with no newline grew `buf` without bound. Cut a line at
            # PS5_LOG_LINE_MAX and drop the rest of it up to the next newline.
            if len(buf) > PS5_LOG_LINE_MAX and b"\n" not in buf:
                ps5_log_add(buf[:PS5_LOG_LINE_MAX].decode("utf-8", "replace").strip() + " [cut]", src=ip)
                buf = b""
                n += 1
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                s = line[:PS5_LOG_LINE_MAX].decode("utf-8", "replace").strip()
                if s:
                    ps5_log_add(s, src=ip)
                    n += 1
            if n >= PS5_LOG_CONN_MAX:
                ps5_log_add("[PS5-log] %s sent %d lines on one connection - closing it" % (ip, n), src=ip)
                buf = b""
                break
        if buf.strip():
            ps5_log_add(buf[:PS5_LOG_LINE_MAX].decode("utf-8", "replace").strip(), src=ip)
    except OSError:
        pass
    finally:
        try:
            conn.close()
        except OSError:
            pass


def start_ps5_log_listener(host="0.0.0.0", port=9097):
    def serve():
        srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            srv.bind((host, port))
        except OSError as e:
            print("[PS5-log] cannot bind :%d (%s) - is it already running?" % (port, e))
            return
        srv.listen(16)
        print("[PS5-log] listening on :%d (on-console payloads report here)" % port)
        while True:
            try:
                conn, addr = srv.accept()
            except OSError:
                break
            threading.Thread(target=_ps5_log_conn, args=(conn, addr), daemon=True).start()
    threading.Thread(target=serve, daemon=True).start()


# A COMPANION THAT IS NOT THE REAL ONE MUST LEAVE THE CONSOLES ALONE.
#
# The two-console test suite starts a whole companion on its own port so it can drive real routes.
# That companion also did everything a companion does in the background - and one of those things
# is to tell every console "the PC is at 10.0.0.76:8791", every eight seconds.
#
# The console keeps that address. The suite then exits, 8791 dies, and the console goes on handing
# the page a PC that is not there: measured on the owner's PS5, which was still offering
# http://10.0.0.76:8791 long after the test that put it there had gone. The page only notices when
# the real PC is briefly unavailable - during a rebuild, say - and then walks its candidate list
# into the dead one, which is exactly what the owner saw in the console's browser.
#
# So a test instance announces nothing, installs nothing and repairs nothing. It still serves every
# route, which is all a test needs. One environment variable, read once, because a flag that has to
# be threaded through six call sites is a flag someone will forget at the seventh.
PMS_TEST_MODE = bool(os.environ.get("PMS_TEST_MODE"))


def start_pc_register_thread(cfg, fleet):
    if PMS_TEST_MODE:
        print(" test mode: not announcing this PC to any console")
        return
    """Push our LAN address to the on-console shop server so the PS5 auto-finds the PC (2-way connect).
    Fire-and-forget; harmless when the on-console server isn't running."""
    port = cfg["companion"]["port"]
    # console.shop_port is the one name every other path uses for the ELF's port; this thread
    # alone read a key called onconsole_port that nothing else read or wrote, so changing the
    # port in config left PC registration on 8710. The old key still works as a fallback.
    con = cfg.get("console", {}) or {}
    oc_port = int(con.get("shop_port") or con.get("onconsole_port") or 8710)

    def loop():
        while True:
            for c in list(fleet.consoles):
                # Per console, not once for all of them. On a dual-homed PC (Ethernet on the
                # console's subnet, Wi-Fi holding the default route) lan_ip() returns the Wi-Fi
                # address, so the console was told to reach us on an address it may have no route
                # to - while the install URL, built with companion_ip_for(), correctly used the
                # Ethernet one. The console's own UI then pointed at the wrong PC.
                ip = companion_ip_for(c.get("ip"))
                # Only when the console is there. The cached TCP probe costs nothing; a request
                # to a switched-off console blocked this thread for the full 2 s every 8 s.
                try:
                    b = fleet.bridge(c.get("id"))
                    if b is not None and not b.up():
                        continue
                except Exception:
                    pass
                try:
                    urllib.request.urlopen(
                        # State our version too: the console ranks companions by it, and until
                        # now it could only learn a version from a federation document read during
                        # a library merge - which a warm peer cache skips entirely.
                        "http://%s:%d/api/register-pc?ip=%s&port=%d&ver=%s"
                        % (c["ip"], oc_port, ip, port, VERSION),
                        timeout=2).read()
                except Exception:
                    pass
            time.sleep(8)

    threading.Thread(target=loop, daemon=True).start()


def run_tray(port):
    """Hidden background app: show a system-tray icon (right-click → Quit). Returns False if pystray
    isn't available so the caller just blocks instead."""
    try:
        import pystray
        from PIL import Image
        import webbrowser
    except Exception:
        return False
    try:
        img = Image.open(os.path.join(WEB_DIR, "assets", "icon.png"))
    except Exception:
        from PIL import Image as _I
        img = _I.new("RGBA", (64, 64), (18, 18, 18, 255))

    def do_open(icon, item):
        webbrowser.open("http://localhost:%d/" % port)

    def do_quit(icon, item):
        icon.stop()
        os._exit(0)

    menu = pystray.Menu(
        pystray.MenuItem("Open PKG MUTANT SHOP", do_open, default=True),
        pystray.MenuItem("Quit", do_quit),
    )
    pystray.Icon("pkgmutantshop", img, "PKG MUTANT SHOP · By XavyProd", menu).run()
    return True


def sweep_temp_dbs(max_age_sec=3600):
    """Delete leftover pms_* temp DB pulls (bgft/app.db copies) + orphaned atomic-write temps older than
    max_age from the temp dir + companion dir. [B4]"""
    now = time.time()
    for d in (tempfile.gettempdir(), HERE):
        try:
            for fn in os.listdir(d):
                if not (fn.startswith("pms_") or fn.startswith(".pms_tmp_")):
                    continue
                p = os.path.join(d, fn)
                try:
                    if now - os.path.getmtime(p) > max_age_sec:
                        os.remove(p)
                except OSError:
                    pass
        except OSError:
            pass


def library_signature(cfg):
    """Cheap fingerprint of the library folders: sorted (path, size, mtime) for every .pkg/.ffpfsc. Changes
    only when a file is added / removed / modified — so we auto-rescan only when something really changed."""
    sig = []
    for root in cfg.get("library", {}).get("local_paths", []):
        if not root or not os.path.isdir(root):
            continue
        for dirpath, _dirs, files in os.walk(root):
            # A game stored as a FOLDER. scan() lists these (is_game_folder), but the fingerprint
            # only looked at files, so an unpacked dump dropped in - or deleted - never changed
            # it and the title appeared (or lingered) until a manual rescan. Summarise the whole
            # tree as (files, bytes, newest mtime), the same figures the scan sizes it by, and
            # take it out of the walk exactly as scan() does.
            claimed = []
            for d in list(_dirs):
                full = os.path.join(dirpath, d)
                if not is_game_folder(full, d):
                    continue
                n = total = newest = 0
                for dp, _dd, fs in os.walk(full):
                    for f in fs:
                        try:
                            st = os.stat(os.path.join(dp, f))
                        except OSError:
                            continue
                        n += 1
                        total += st.st_size
                        newest = max(newest, int(st.st_mtime))
                sig.append((full, total, newest, n))
                claimed.append(d)
            if claimed:
                _dirs[:] = [d for d in _dirs if d not in claimed]
            for fn in files:
                low = fn.lower()
                if not (low.endswith(".pkg") or low.endswith(MOUNT_EXTS)):
                    continue
                ap = os.path.join(dirpath, fn)
                try:
                    st = os.stat(ap)
                    sig.append((ap, st.st_size, int(st.st_mtime)))
                except OSError:
                    pass
    return tuple(sorted(sig))


def start_library_watch(httpd):
    """Debounced auto-rescan: poll the library signature; when it changes, wait for it to settle (so we
    never parse a half-copied PKG) then rescan. Gated by maintenance.auto_rescan (default on). [B4]"""
    # Always start the thread; the flag is read INSIDE the loop instead. It used to be read
    # here, once, so switching the setting ON did nothing until the next restart, and switching it
    # OFF did nothing at all - the loop it was meant to stop had already captured the old value.
    m = httpd.cfg.get("maintenance", {})
    interval = max(3, int(m.get("rescan_interval_sec", 10)))
    settle = max(0, int(m.get("rescan_settle_sec", 3)))

    def loop():
        last = library_signature(httpd.cfg)
        while True:
            time.sleep(interval)
            # Re-read every tick, from the LIVE config, so the setting takes effect when it is
            # changed rather than when the app is next started.
            if not httpd.cfg.get("maintenance", {}).get("auto_rescan", True):
                continue
            try:
                sig = library_signature(httpd.cfg)
                if sig == last:
                    continue
                if settle:
                    time.sleep(settle)
                    if library_signature(httpd.cfg) != sig:
                        last = sig          # still changing (big copy in progress) — recheck next tick
                        continue
                httpd.library.scan()        # atomic swap inside; safe for concurrent /library/
                last = sig
                print("[watch] library changed -> rescanned (gen=%d)" % httpd.library.gen)
            except Exception as e:
                print("[watch] %s" % e)

    threading.Thread(target=loop, daemon=True).start()


def start_cheat_sync_thread(httpd):
    """Self-repair the console's cheat library in the background - from a SOURCE checkout only.

    The PS5 ELF embeds the library and writes it to that console itself, so this loop exists for the
    PS4 and for repair. THE FROZEN BUILD HAS A LIBRARY TOO - the exe ships cheats-library.zip and
    _unpack_cheat_library() expands it beside the exe at startup (see CHEATS_DIR at the top) - so the
    old claim here, that a frozen build finds nothing local and sends nothing, was wrong. What is true
    is that it sends nothing until that expansion has finished, which is one of the reasons it waits
    before its first pass. From source it fills a wiped /data with no user action. It is incremental (only files a COMPLETE listing proves missing) so the normal case
    costs a few directory listings and sends nothing, and it is time-budgeted so it can never
    monopolise the link that installs depend on: it stops after the budget and finishes on a
    later pass. sync_cheat_library() holds _CHEAT_SYNC_LOCK, so a manual POST cannot overlap it.
    """
    def loop():
        time.sleep(25)                       # let the library scan and the first UI poll settle
        while True:
            try:
                # EVERY CONSOLE, not the PS5 by name. This used to ask for a PS5 specifically,
                # because cheats were a PS5 capability and a PS4 had nothing to run them with.
                # That is no longer true: the PS4 payload carries the same engine now. It is also
                # the console that NEEDS this most - the PS5's ELF ships the whole library inside
                # it and this sync only tops up what was added since that build, while the PS4's
                # library arrives entirely through here.
                #
                # engine_available() is still the gate, so a console that cannot use them is
                # skipped on its own say-so rather than on a guess about its platform.
                fleet = getattr(httpd, "fleet", None)
                blist = list(fleet.bridges.values()) if fleet else [_bridge_for(httpd)]
                for bridge in blist:
                    if bridge is None or not os.path.isdir(CHEATS_DIR):
                        continue
                    if not bridge.engine_available():
                        continue
                    st = bridge.cheat_library_status()
                    missing = st.get("missing_total")
                    if missing:
                        print("[cheats] %s is missing %d file(s) — syncing"
                              % ((bridge.c or {}).get("name") or "console", missing))
                        r = bridge.sync_cheat_library(log=lambda m: print("[cheats] %s" % m),
                                                      budget=90.0)
                        print("[cheats] sent=%s failed=%s partial=%s in %ss"
                              % (r.get("sent"), r.get("failed"), r.get("partial"), r.get("seconds")))
            except Exception as e:
                print("[cheats] sync skipped: %r" % e)
            time.sleep(900)                  # re-check every 15 min; a healthy console is a no-op
    threading.Thread(target=loop, daemon=True).start()


# start_dpi_probe_thread() lived here. It polled every console's :12800 every 20 seconds to keep
# a "live / wedged / down" cache warm for the UI. There is no daemon on 12800 any more, so the
# thread had nothing to measure - and it was the last caller of dpi_host_kind() that no config
# setting could switch off, which made it the one thing keeping the whole third-party probe alive.


def installed_ids(apps):
    """The title ids a console really HAS, from its app list. None when it gave no list.

    INSTALLED HAS TO MEAN INSTALLED. A console lists titles it does not have - measured on the
    owner's PS4, 21 titles of which 5 (THE PLAYROOM, Game Streaming, Share Play, PlayStation Video,
    PS5 Remote Play) are system entries with no data on disk - and a deleted app keeps its row for a
    while before the console drops it. installed_titles() returns every id MENTIONED, which is the
    right answer for the install-confirm loop waiting for a row to appear and the wrong one for a
    panel drawing the word "Installed" next to a thing.

    install_status is 0 for a title whose data is really in place, and it is set on both platforms:
    the PS5 reads it from app.db, the PS4 path sets it from the payload's own `installed` flag.
    """
    if apps is None:
        return None
    return {str(a.get("title_id") or "").upper() for a in apps if not a.get("install_status")}


def probe_cache_hit(cache, key, now, ttl=4.0):
    """A recent console probe to reuse, or None when it is time to ask again.

    THE AGE OF A CACHED ANSWER IS THE AGE OF THE ANSWER, NOT OF THE LAST LOOK AT IT - and the
    caller must therefore write the entry back only when it has actually re-measured. The panel's
    cache used to be re-stored with a fresh timestamp on every request, including the ones that
    merely copied the previous values out, so each poll reset the clock on data nobody had re-read.
    The panel polls every three seconds and this expires after four, so the clock was reset before
    it could ever run out: the console was asked once, when the panel was opened, and never again
    while anybody kept looking at it.

    Measured consequence: the owner deleted FPKGi from both consoles, and the PC with the panel
    open went on reporting it installed while the console's own library did not list the title at
    all - and a second PC, which nobody was watching, answered correctly.
    """
    hit = cache.get(key)
    if hit and (now - hit[0]) < ttl:
        return hit
    return None


# Set at boot when the files this exe carries are not where they should be. See check_embedded().
_degraded = ""


def check_embedded():
    """Are the files this build carries actually on disk? Returns a sentence, or "" when fine.

    A ONE-FILE BUILD UNPACKS ITSELF INTO %TEMP% BEFORE ANY OF THIS RUNS, and that can come out
    incomplete. Measured here on 2026-10-01: a build started by its own self-update came up with an
    extraction folder that had no `web/` in it at all - no index.html, no catalogue, an empty assets
    directory - while the exe itself was byte-for-byte correct and a plain restart was fine.

    WHAT THAT COSTS IS NOT OBVIOUS, WHICH IS THE PROBLEM. catalog() returns {} when the file is
    missing, by design, so the app kept running and answered every request - with the whole curated
    layer gone. Every payload lost its repo, so the Updates list silently had nothing to offer and
    the app could no longer see its own new version. It looked like a bug in the update lane and it
    was a bug in a launch.

    So this is checked once, said plainly in the log, and reported to every device. It does not
    exit: a working-but-degraded app the owner can see is better than one that will not start.
    """
    if not getattr(sys, "frozen", False):
        return ""
    want = [os.path.join(WEB_DIR, "index.html"),
            payload_engine.catalog_path(WEB_DIR),
            os.path.join(WEB_DIR, "assets", "logo.png")]
    missing = [p for p in want if not os.path.isfile(p)]
    if not missing:
        return ""
    print("[boot] extraction folder: %s" % (getattr(sys, "_MEIPASS", "?") or "?"))
    return ("This copy did not unpack completely - %d of its own files are missing from %s. "
            "Close the app and open it again; nothing is wrong with the download."
            % (len(missing), os.path.dirname(os.path.dirname(missing[0]))))


AFTER_UPDATE_FLAG = "--after-update"
REPLACING_FLAG = "--replacing"
_relaunching = False      # see relaunch_self(): one replacement, however many tiles ask
# Set once the exe ON DISK is a newer build than the one running. From then on every device that
# asks is told, and the only thing left is picking a moment to restart.
_update_ready = {}
_update_lock = threading.Lock()


def our_repo(cfg):
    """The GitHub project this app updates itself from, read from our own catalogue entry.

    Not a constant typed here: the catalogue already carries it (`ours: true` + `repo`), it is what
    the Updates list asks for every other payload, and two places naming the same repository is how
    one of them ends up pointing at an account nobody uses any more.
    """
    try:
        cat, _sig = payload_engine.live_catalog(cfg, WEB_DIR)
        for it in (cat.get("items") or []):
            if it.get("ours") and it.get("repo"):
                return str(it["repo"])
    except Exception:
        pass
    return ""


def app_update_state():
    """What every device is told about THIS app's own update. {} when there is nothing to say."""
    with _update_lock:
        return dict(_update_ready)


def self_update_once(cfg, force=False, log=None):
    """Fetch and install a newer build of this app, if there is one. Returns (ok, sentence).

    WHY THIS EXISTS, AND WHY IT IS NOT THE TILE. The Payloads panel offers our own entry like any
    other payload - but what it compares is the ELF SITTING IN THE OWNER'S FOLDER, not the app doing
    the comparing. So a PC whose folder was already current was told everything matched while the
    exe it was running was three releases old, and the only way out was to copy the file across by
    hand or go to the releases page. That is exactly how the owner had been updating their second
    PC, and it is what this removes.

    Nothing is installed anywhere else: the console's ELF stays a separate, deliberate press.
    """
    say = log or (lambda m: print(m))
    if not payload_engine.running_exe():
        # A console, or a source run. There is no exe here to replace and nothing to download.
        return False, "This is not the packaged app, so there is nothing to update."
    with _update_lock:
        if _update_ready.get("version"):
            return True, "A newer build is already in place - restart to use it."
    repo = our_repo(cfg)
    if not repo:
        if _degraded:
            return False, _degraded          # the catalogue is one of the files that did not unpack
        return False, "There is no upstream recorded for this app."
    rel = payload_engine.github_latest(repo, force=force, token=payload_engine.gh_token(cfg))
    if rel.get("error"):
        return False, "Could not reach GitHub (%s)." % rel["error"]
    tag = str(rel.get("tag") or "")
    if not payload_engine.newer_than(tag, VERSION):
        return False, "This is already the newest build."
    ok, msg = payload_engine.update_self(cfg, rel, log=say)
    if not ok:
        return False, msg
    with _update_lock:
        _update_ready.update({"version": tag.lstrip("vV"), "was": VERSION, "at": int(time.time())})
    say("[update] %s is on disk; this build is %s" % (tag, VERSION))
    if (cfg.get("updates") or {}).get("auto_restart", False):
        schedule_relaunch(cfg["companion"]["port"], log=say)
        return True, "Updated. The app is restarting itself now."
    return True, "Version %s is ready. Restart the app to use it." % tag.lstrip("vV")


def start_self_update_watch(httpd):
    """Check for a newer build of this app on a slow clock, and install it when there is one.

    Only on a packaged PC build - a console has no exe to replace, and a source run is somebody
    working on the code. The first check waits, because startup already has a library scan, a LAN
    sweep and two consoles to talk to, and none of them should queue behind a GitHub request.
    """
    cfg = httpd.cfg
    if not (cfg.get("updates") or {}).get("self_update", True):
        return
    if not payload_engine.running_exe():
        return
    every = max(1.0, float((cfg.get("updates") or {}).get("check_every_hours", 6) or 6)) * 3600.0

    def loop():
        time.sleep(120)
        while True:
            try:
                ok, msg = self_update_once(cfg, force=True)
                if ok:
                    print("[update] %s" % msg)
            except Exception as e:
                print("[update] check skipped: %r" % e)
            with _update_lock:
                done = bool(_update_ready.get("version"))
            if done:
                return          # it is on disk; nothing further to look for
            time.sleep(every)

    threading.Thread(target=loop, daemon=True).start()
_relaunch_timer = None    # see schedule_relaunch(): armed, and pushed back while work continues
_relaunch_lock = threading.Lock()


HANDOVER_GRACE_SEC = 20.0

# Extraction folders this build has handed over from, by absolute path. See note_my_extraction().
MEI_NOTE = os.path.join(HERE, "extraction-leftovers.json")


def note_my_extraction():
    """Write down the extraction folder this process is leaving, for its successor to remove.

    MEASURED ON THIS MACHINE, 2026-10-01: 88.9 MB across 169 folders in %TEMP% that the sweeper
    below could not touch, including two from today with the SAME signature - 17 files, 13.6 MB,
    no web/ - at 14:00:57 and 22:15:43, both moments a self-update restarted the app. That is one
    rmtree interrupted at the same point twice: the bootloader was deleting its own folder and was
    force-killed partway (see _clear_replaced). What is left has lost the marker files the sweeper
    proves ownership with, so it could never be claimed again, by anything.

    Being TOLD the path removes the guessing entirely. Nothing is inferred from a folder's name or
    contents, so another program's extraction folder can never be a candidate.
    """
    mine = getattr(sys, "_MEIPASS", "") or ""
    if not (getattr(sys, "frozen", False) and mine):
        return
    try:
        old = []
        if os.path.isfile(MEI_NOTE):
            with io.open(MEI_NOTE, encoding="utf-8") as fh:
                old = json.load(fh) or []
        if not isinstance(old, list):
            old = []
        keep = [p for p in old if isinstance(p, str) and p != mine][-19:] + [mine]
        _atomic_write_json(MEI_NOTE, keep, indent=1)
    except Exception:
        pass            # a note for the tidy-up must not be able to stop a handover


def _pid_alive(pid):
    """Is this pid in the task list? True when it is, or when we could not find out."""
    try:
        out = subprocess.run(["tasklist", "/FI", "PID eq %d" % pid, "/NH"],
                             stdout=subprocess.PIPE, stderr=subprocess.DEVNULL,
                             timeout=15).stdout
        return (b"%d" % pid) in (out or b"")
    except Exception:
        return True


def _clear_replaced(argv):
    """End the processes this build was started to replace. Only the ones it was NAMED.

    Called only when we were launched with --replacing, so these are the pid of the build that
    handed over and its bootloader parent - no enumeration, no guessing which copies of this exe
    belong to somebody else, and nothing to do when the app was started normally.

    Both are usually gone already: the one that handed over exits on its own. This is for the
    bootloader that outlives it (see relaunch_self), because while it lives it holds <exe>.old open
    and the next self-update cannot rename over it.

    IT IS GIVEN TIME TO GO BY ITSELF, AND THAT IS THE POINT OF THE WAIT. A one-file bootloader
    deletes its ~56 MB extraction folder on the way out, and this used to fire the instant the port
    came free - about three seconds after the handover, which is the middle of that delete. Two
    half-deleted folders from one day prove it: 17 files and 13.6 MB left in each, identical,
    because an interrupted rmtree stops in the same place every time. Forcing is still here for the
    bootloader that genuinely wedges (measured once at nine minutes), so nothing is lost by asking
    politely for twenty seconds first.

    All of it on a thread: the point of this build starting is to serve, not to watch the last one
    leave, and sweep_old_exe() already retries in the background for the file that is held.
    """
    try:
        i = argv.index(REPLACING_FLAG)
        raw = argv[i + 1]
    except (ValueError, IndexError):
        return
    pids = []
    for part in str(raw).split(","):
        try:
            pid = int(part)
        except ValueError:
            continue
        if pid > 0 and pid != os.getpid():
            pids.append(pid)
    if not pids:
        return

    def clear(every=1.0):
        grace = HANDOVER_GRACE_SEC
        left = list(pids)
        end = time.time() + grace
        while left and time.time() < end:
            left = [p for p in left if _pid_alive(p)]
            if not left:
                print("[boot] the build that handed over has finished closing")
                return
            time.sleep(min(every, max(0.05, grace / 4.0)))
        for pid in left:
            try:
                # taskkill, not os.kill: on Windows os.kill() with any signal is TerminateProcess,
                # and a stale pid that has been reused would be somebody else's program. No /T
                # either - this build is a descendant of that tree.
                subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
                print("[boot] pid %d had not closed after %gs - ended it, so <exe>.old can be "
                      "replaced" % (pid, grace))
            except Exception:
                pass

    threading.Thread(target=clear, daemon=True).start()


def _wait_for_port_release(port, secs=90.0):
    """Wait until nothing answers on `port`. True if it came free.

    THE OLD PROCESS IS STILL RUNNING WHEN THIS ONE STARTS, and that is deliberate. A self-update
    renames the running exe aside, puts the new build in its place and launches it; the two overlap
    for as long as the old one needs to finish answering the request that triggered it and shut its
    sockets. Without waiting here, the new build would walk straight into the single-instance guard
    below, print "already running" and exit - leaving the owner on the old build, with the update
    on disk and nothing to show for it. That is the same shape of silent no-op this whole lane
    exists to stop.
    """
    end = time.time() + secs
    while time.time() < end:
        if not _already_running(port):
            return True
        time.sleep(0.5)
    return False


def schedule_relaunch(port, log=None, delay=8.0):
    """Restart once the owner has stopped taking updates, not in the middle of them.

    "Update all" walks the list one at a time, and our own entry is one row of it. Restarting the
    moment that row succeeds cuts the chain off: measured here, the PS5 row reported success and
    restarted, and the PS4 row that was already in flight died with the connection. Nothing was
    lost - the remaining updates are still offered afterwards - but the owner pressed one button
    and had to press it again, which is not what "it updates itself" should mean.

    So the restart is armed rather than performed, and every further update request pushes it back.
    The app goes when the panel has been quiet for `delay`.
    """
    global _relaunch_timer
    with _relaunch_lock:
        if _relaunching:
            return True
        if _relaunch_timer is not None:
            _relaunch_timer.cancel()
        _relaunch_timer = threading.Timer(delay, relaunch_self, args=(port,), kwargs={"log": log})
        _relaunch_timer.daemon = True
        _relaunch_timer.start()
    return True


def defer_relaunch(delay=8.0):
    """Push an armed restart back, because something else is happening. No-op if none is armed."""
    global _relaunch_timer
    with _relaunch_lock:
        if _relaunching or _relaunch_timer is None:
            return
        _relaunch_timer.cancel()
        _relaunch_timer = threading.Timer(delay, relaunch_self,
                                          args=(_relaunch_timer.args[0],),
                                          kwargs=_relaunch_timer.kwargs)
        _relaunch_timer.daemon = True
        _relaunch_timer.start()


def relaunch_self(port, log=None):
    """Start the replacement build and stand down. True if the new one was launched.

    Only ever called after update_self() has actually put a new exe in place, so the file this
    launches is the new one - the running image is the renamed .old, which the new build sweeps up
    when it starts.

    Nothing is killed. This process exits on its own a moment later, AFTER the reply to the request
    that asked for the update has gone out; the new build waits for the port rather than racing for
    it.
    """
    say = log or (lambda m: None)
    exe = payload_engine.running_exe()
    if not exe:
        return False
    # ONCE. Our own entry appears once per console, so "Update all" reaches here twice in a second;
    # a second launch would start a replacement that then loses the race for the port to the first
    # one and exits through the single-instance guard - harmless, and an extra window flashing up
    # on the owner's desktop for no reason.
    global _relaunching
    if _relaunching:
        return True
    _relaunching = True
    try:
        flags = 0
        if os.name == "nt":
            # Detached and in its own process group: the child must outlive us, and must not take
            # a Ctrl-C meant for the console this one was started from.
            flags = 0x00000008 | 0x00000200      # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        # WHO TO CLEAR UP AFTER, BY NUMBER. This process exits a moment from now, but the exe is
        # a PyInstaller bundle: a bootloader parent starts the Python child that runs all of this,
        # and os._exit() in the child does not always take the parent with it. Measured here after
        # a working self-update - the new build was serving on the port and the OLD build's
        # bootloader was still in the task list, nine minutes later, holding its own image open.
        # Nothing had gone wrong with the restart; the leftover just sat there.
        #
        # It matters because that image is <exe>.old: while it is held, the sweep cannot remove it
        # AND THE NEXT SELF-UPDATE CANNOT RENAME OVER IT - which is exactly the PermissionError this
        # whole lane exists to avoid. Enumerating processes to find it would mean guessing which
        # ones are ours; being told the two numbers does not.
        note_my_extraction()      # for the build starting now to tidy up after this one, by name
        subprocess.Popen([exe, AFTER_UPDATE_FLAG, str(port),
                          REPLACING_FLAG, "%d,%d" % (os.getpid(), os.getppid())],
                         cwd=os.path.dirname(exe) or None,
                         close_fds=True, creationflags=flags)
    except Exception as e:
        say("[update] could not start the new build (%s)" % e.__class__.__name__)
        return False
    say("[update] the new build is starting; this one is closing")

    def _bye():
        # Long enough for the HTTP reply to reach the panel that asked, short enough that the owner
        # does not see two copies in the task list. The new build is waiting on the port, so this
        # delay costs nothing but the wait itself.
        time.sleep(2.5)
        os._exit(0)

    threading.Thread(target=_bye, daemon=True).start()
    return True


def _already_running(port):
    """Is another PKG MUTANT SHOP already serving on this port? Returns its version, or None.

    Asked BEFORE binding. Two copies on one port is not hypothetical - the loser of the bind race
    keeps every background thread running (library rescan, console probes, installed.json writes),
    which is how one process silently overwrites the other's record of what is installed.
    """
    try:
        # 4 s, not 2: a cold /api/health on the running copy has measured 2.3 s (it joins a
        # helper probe and asks the console), and a probe that gives up before the answer arrives
        # is a guard that fails open - the second copy binds and both write installed.json.
        with urllib.request.urlopen("http://127.0.0.1:%d/api/health" % port, timeout=4) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        return str(j.get("version") or "?") if j.get("ok") else None
    except Exception:
        return None


def sweep_mei_leftovers(max_age_sec=24 * 3600):
    """Delete orphaned PyInstaller one-file extraction folders that are provably OURS.

    Every launch of the exe unpacks itself into <temp>/_MEIxxxxxx and removes it on a clean exit;
    a crash, a kill from Task Manager or a power cut leaves the folder (~40 MB each) behind for
    ever. Only when frozen; only folders named _MEI* that hold our own marker files; never the one
    this process is running from; only older than a day. Every error is ignored - a tidy-up must
    not be able to stop the app."""
    if not getattr(sys, "frozen", False):
        return
    mine = os.path.normcase(os.path.realpath(getattr(sys, "_MEIPASS", "") or ""))
    now, n = time.time(), 0

    # FIRST, THE ONES A PREVIOUS BUILD NAMED. These need no marker and no age: a folder we wrote
    # down ourselves is ours by definition, which is the only way to reclaim one whose contents a
    # killed cleanup already took (that is where 88.9 MB of this machine went). Paths that are gone
    # drop off the list; a path still in use by a live copy is left alone and stays on it.
    try:
        named = []
        if os.path.isfile(MEI_NOTE):
            with io.open(MEI_NOTE, encoding="utf-8") as fh:
                named = json.load(fh) or []
        rest = []
        for p in (named if isinstance(named, list) else []):
            if not isinstance(p, str) or not p:
                continue
            try:
                if os.path.normcase(os.path.realpath(p)) == mine:
                    rest.append(p)                      # this build's own; keep the note for later
                    continue
                if not os.path.isdir(p):
                    continue                            # already gone - forget it
                aside = p + ".gone"
                if os.path.isdir(aside):
                    shutil.rmtree(aside, ignore_errors=True)
                os.rename(p, aside)                     # raises while anything inside is open
                shutil.rmtree(aside, ignore_errors=True)
                n += 1
            except OSError:
                rest.append(p)                          # in use by a live copy; leave it alone
            except Exception:
                rest.append(p)
        if rest != named:
            _atomic_write_json(MEI_NOTE, rest, indent=1)
    except Exception:
        pass
    try:
        tmp = tempfile.gettempdir()
        for fn in os.listdir(tmp):
            if not fn.startswith("_MEI"):
                continue
            p = os.path.join(tmp, fn)
            try:
                if not os.path.isdir(p) or os.path.normcase(os.path.realpath(p)) == mine:
                    continue
                if not (os.path.isfile(os.path.join(p, "web", "index.html"))
                        and os.path.isfile(os.path.join(p, "web", "assets", "logo.png"))):
                    continue
                if now - os.path.getmtime(p) < max_age_sec:
                    continue
                shutil.rmtree(p, ignore_errors=True)
                n += 1
            except Exception:
                pass
    except Exception:
        pass
    if n:
        print("[boot] removed %d leftover extraction folder(s) from an earlier run" % n)


def main():
    _log_open()                       # before anything else: the boot lines are worth keeping
    cfg = load_config()
    # SINGLE INSTANCE, before any side effect. This used to be asked after the library scan (which
    # RENAMES unsafe .pkg names), the LAN sweep (which can save config.json) and the queue's
    # workers had all started - so a second launch rewrote files the live copy owns before
    # deciding to exit. On Windows a second bind can also succeed while the first process keeps
    # running every background thread it owns, and both then write installed.json.
    # STARTED BY AN UPDATE? Then the build being replaced is probably still finishing up, and the
    # single-instance guard below would read that as "already running" and send this copy away.
    if AFTER_UPDATE_FLAG in sys.argv[1:]:
        _p = cfg["companion"]["port"]
        print("[boot] started by an update - waiting for the previous build to let go of port %d"
              % _p)
        if _wait_for_port_release(_p):
            print("[boot] it has; taking over")
        else:
            print("[boot] it has not, after 90s - carrying on and letting the guard below decide")
        _clear_replaced(sys.argv)
    _other = _already_running(cfg["companion"]["port"])
    if _other:
        print("[boot] PKG MUTANT SHOP v%s is ALREADY running on port %d - not starting a second "
              "copy. Close the other one first, or change companion.port."
              % (_other, cfg["companion"]["port"]))
        try:
            # imported locally: the module-level imports here are deliberately minimal, and the
            # other caller in this file does the same.
            import webbrowser as _wb
            _wb.open("http://127.0.0.1:%d/" % cfg["companion"]["port"])
        except Exception:
            pass
        return 0
    os.makedirs(ICON_DIR, exist_ok=True)
    cfg = _adopt_library_root(cfg)
    # BEFORE THE FIRST SCAN, because everything downstream reads files this is about to look for.
    global _degraded
    _degraded = check_embedded()
    if _degraded:
        print("=" * 78)
        print("[boot] %s" % _degraded)
        print("=" * 78)

    ensure_all_folders(cfg)              # before the scan, so the first scan sees a real tree
    # THE PREVIOUS BUILD, IF THIS START IS THE ONE AFTER AN UPDATE. It could not be deleted at the
    # moment of the swap, because at that moment it was the file the process was executing from.
    payload_engine.sweep_old_exe()
    sweep_mei_leftovers()
    library = Library(cfg)
    library.scan()
    fleet = Fleet(cfg)
    # If the saved PS5 isn't reachable, auto-find it on the LAN so the app just works with no config.
    try:
        b0 = fleet.bridge(fleet.ids()[0]) if fleet.consoles else None
        # A console that answers on its shop port, or whose Payload Manager (8084) answers, IS
        # there - the ELF merely is not loaded yet. That used to trigger the whole /24 sweep on
        # every boot with the PS5 asleep or before the payload was loaded (pms.log: three
        # "Auto-found" lines in one day for the same, already configured, address).
        present = bool(b0 and b0.ip and (b0.engine_available() or b0.up()
                                          or _port_open(b0.ip, cfg.get("dpi", {}).get("pldmgr_port", 8084), 0.6)))
        # AUTO-FIND IS FOR THE ZERO-CONFIG CASE ONLY.
        #
        # It exists so a fresh install finds the console with nothing filled in, and the way it does
        # that is to write ONE address. That was harmless while there could only be one console.
        # With two - which is exactly what PS4 support makes ordinary - the write below stamped the
        # discovered address onto EVERY entry, so a successful scan that found the PS4 also rewrote
        # the PS5's address to the PS4's, in config.json, permanently. Anything the user has
        # deliberately set up is left exactly as they set it.
        if len(cfg.get("consoles") or []) > 1:
            present = True
            print(" %d consoles configured - not auto-finding; their addresses are yours to set"
                  % len(cfg["consoles"]))
        if not present:
            found = discover_ps5(cfg)
            if found:
                newip = found[0]["ip"]
                confirmed = bool(found[0].get("confirmed"))
                # Only rewrite config.json when the address actually changed. A console that is
                # merely not loaded yet used to get "Auto-found" and a config save on every boot.
                unchanged = (cfg.get("ps5_ip") == newip and
                             all(c.get("ip") == newip for c in (cfg.get("consoles") or [])))
                plat = str(found[0].get("platform") or "").lower()
                # Only a PS5 is recorded as ps5_ip. That key is the legacy single-console setting and
                # a great deal reads it; pointing it at a PS4 would be a lie about which console the
                # PS5-only lanes should talk to.
                if plat != "ps4":
                    cfg["ps5_ip"] = newip
                added = False
                if cfg.get("consoles"):
                    # Exactly one entry here (see the guard above).
                    #
                    # A DIFFERENT KIND OF CONSOLE IS A SECOND CONSOLE, NOT A REPLACEMENT. This used
                    # to overwrite the one entry's address and platform unconditionally, so a
                    # PS5 that was merely switched off, with the PS4 awake on the LAN, had its
                    # config entry quietly turned into the PS4 - address, platform and all - and
                    # /api/health then reported the PS4's address as ps5_ip. Measured: a config
                    # holding one PS5 at 10.0.0.99 came back as a PS4 at 10.0.0.87.
                    #
                    # Overwriting is still right for the case it was written for - the SAME console
                    # having moved to a new address. It is only wrong across platforms.
                    c0 = cfg["consoles"][0]
                    have = str(c0.get("platform") or "").lower()
                    if plat and have and plat != have:
                        used = {str(c.get("id") or "") for c in cfg["consoles"]}
                        cid = plat
                        n = 0
                        while cid in used:
                            n += 1
                            cid = "%s-%d" % (plat, n)
                        cfg["consoles"].append({"id": cid,
                                                "name": plat.upper(),
                                                "ip": newip, "platform": plat,
                                                "ftp_port": cfg["ftp"].get("port", 2121)})
                        added = True
                    else:
                        c0["ip"] = newip
                        if plat:
                            c0["platform"] = plat
                else:
                    # NOTHING CONFIGURED AT ALL - so take EVERY console that confirmed, not the
                    # first one. discover_ps5() returns them all, sorted confirmed-first and then
                    # by ADDRESS, and this used to keep found[0]: with both machines awake a PS4 at
                    # .87 sorted ahead of a PS5 at .99 and the PS5 was dropped. Measured on a bare
                    # exe - one file, no config.json, which is exactly what gets carried to a second
                    # PC - ps5_online came back false with the PS5 answering on 8710 and 8084.
                    #
                    # A guess is still never saved: only a host that CONFIRMED it is a console gets
                    # an entry, which is the same rule the single-console path has always used.
                    made, used_ids = [], set()
                    for f in found:
                        if not f.get("confirmed"):
                            continue
                        fp = str(f.get("platform") or "").lower()
                        base_id = "ps4" if fp == "ps4" else "ps5"
                        cid, n2 = base_id, 0
                        while cid in used_ids:
                            n2 += 1
                            cid = "%s-%d" % (base_id, n2)
                        used_ids.add(cid)
                        made.append({"id": cid, "name": base_id.upper(),
                                     "ip": f["ip"], "platform": fp or "ps5",
                                     "ftp_port": cfg["ftp"].get("port", 2121)})
                    if not made:
                        # None of them would confirm. Keep the old behaviour exactly: try the best
                        # guess for this run and do not write it down.
                        made = [{"id": "ps4" if plat == "ps4" else "ps5",
                                 "name": "PS4" if plat == "ps4" else "PS5",
                                 "ip": newip, "platform": plat or "ps5",
                                 "ftp_port": cfg["ftp"].get("port", 2121)}]
                    cfg["consoles"] = made
                    # The two legacy address keys still drive a great deal, so they follow the
                    # list rather than the first thing discovered. ps5_ip must never point at a
                    # PS4 - that is a lie about which console the PS5-only lanes should talk to.
                    for c in made:
                        if str(c.get("platform")).lower() == "ps4":
                            cfg.setdefault("ps4_ip", "")
                            if not cfg.get("ps4_ip"):
                                cfg["ps4_ip"] = c["ip"]
                        elif not cfg.get("ps5_ip"):
                            cfg["ps5_ip"] = c["ip"]
                    if len(made) > 1:
                        print(" Auto-found %d consoles: %s"
                              % (len(made), ", ".join("%s at %s" % (c["name"], c["ip"])
                                                      for c in made)))
                # ORDER THEM AGAIN. reconcile_consoles() runs inside load_config(), which is
                # long finished by the time auto-find writes this list - so a fresh install came up
                # with whatever order the scan returned, which is by ADDRESS, which put a PS4 at
                # .87 in front of a PS5 at .99. consoles[0] still means "the console" to a great
                # many routes, and it is supposed to be the PS5 on a fleet that has one.
                reconcile_consoles(cfg)
                fleet = Fleet(cfg)
                # Only a host that CONFIRMED it is a console gets written to disk. A guess is good
                # enough to try for this run, but must not outlive it - that is how the console
                # address silently became a peer PC's and stayed there.
                label = "PS4" if plat == "ps4" else "PS5"
                if confirmed and unchanged and not added:
                    print(" %s at %s is not answering yet - keeping that address" % (label, newip))
                elif confirmed:
                    try:
                        save_config(cfg)
                    except Exception:
                        pass
                    print(" Auto-found %s at %s%s" % (label, newip,
                          " - added as a second console; the one you had is untouched" if added else ""))
                else:
                    print(" Trying %s as the console for this run (it did not confirm it is one, "
                          "so this is not being saved)" % newip)
    except Exception as e:
        print(" PS5 auto-find skipped: %s" % e)

    # A PLATFORM THIS PC HAS NEVER HEARD OF, on a network where it is answering.
    #
    # The block above only runs when nothing is configured or the one configured console is not
    # answering. On a PC that already knows a PS5, and whose PS5 is awake, it is skipped entirely -
    # so a PS4 on the same network stays invisible for ever. That is what the owner's two PCs
    # showed: identical exe, identical network, and one of them had no console picker, no PS4 drive
    # tile and no PS4 anywhere, because its config named a PS5 and nothing ever looked again.
    #
    # Deliberately narrow. It ADDS ONLY: nothing already configured is renamed, re-addressed or
    # removed, and it fires only for a platform with NO entry at all - so a console the owner took
    # out of Settings does not reappear while another of its kind is still listed. And only a host
    # that CONFIRMED it is a console is written down, which is the same rule the block above uses.
    try:
        have_plat = {str(c.get("platform") or "").lower() or "ps5"
                     for c in (cfg.get("consoles") or [])}
        if cfg.get("consoles"):                      # nothing configured is the case above
            missing = [f for f in discover_ps5(cfg)
                       if f.get("confirmed")
                       and (str(f.get("platform") or "").lower() or "ps5") not in have_plat
                       and not any(c.get("ip") == f["ip"] for c in cfg["consoles"])]
            added_new = []
            for f in missing:
                fp = str(f.get("platform") or "").lower() or "ps5"
                if fp in have_plat:                  # only the first of each kind
                    continue
                have_plat.add(fp)
                used = {str(c.get("id") or "") for c in cfg["consoles"]}
                cid, n3 = fp, 0
                while cid in used:
                    n3 += 1
                    cid = "%s-%d" % (fp, n3)
                cfg["consoles"].append({"id": cid, "name": fp.upper(), "ip": f["ip"],
                                        "platform": fp,
                                        "ftp_port": cfg.get("ftp", {}).get("port", 2121)})
                if fp == "ps4" and not cfg.get("ps4_ip"):
                    cfg["ps4_ip"] = f["ip"]
                elif fp != "ps4" and not cfg.get("ps5_ip"):
                    cfg["ps5_ip"] = f["ip"]
                added_new.append("%s at %s" % (fp.upper(), f["ip"]))
            if added_new:
                reconcile_consoles(cfg)
                fleet = Fleet(cfg)
                try:
                    save_config(cfg)
                except Exception:
                    pass
                print(" Found a console this PC had never seen: %s - added, and the ones you "
                      "already had are untouched" % ", ".join(added_new))
    except Exception as e:
        print(" second-console check skipped: %s" % e)

    # THE ADDRESSES ARE MADE TRUE BEFORE ANYTHING USES THEM. The two passes above handle "nothing
    # configured" and "a platform I have never had"; this one handles the case they both miss and
    # the owner actually hit - a console that is configured, switched on, and somewhere else now.
    try:
        track_consoles(cfg, fleet, log=lambda m: print("[consoles]%s" % m))
    except Exception as e:
        print(" console check skipped: %s" % e)

    dns = DnsBlocker(cfg)
    dns.start()

    # THE ICON REPAIRS ITSELF, because the console cannot always repair it alone.
    #
    # A PS4 with PlayStation Network blocked - which is the configuration this app asks for - has a
    # download service that refuses to register a package the CONSOLE serves to itself: measured
    # 0x80991404, klog "CDN Auth Expired", for both 127.0.0.1 and the console's own LAN address. The
    # identical package offered by THIS PC registers first time and installs completely, with the
    # boot-parameter files and a launch ticket. So the console can install its own icon but cannot
    # always make it openable, and the PC can.
    #
    # Hence this: while a PS4 is reachable, if its icon is not launchable, install it from here. Once
    # per console per REPAIR_EVERY seconds at most, so a console that cannot be fixed is not hammered,
    # and only ever when the console itself says the icon will not open.
    def tile_watchdog():
        # Installing the dashboard app is a real change to a real console. A test instance does not
        # get to make it - see PMS_TEST_MODE.
        if PMS_TEST_MODE:
            return
        REPAIR_EVERY = 600
        last = {}
        while True:
            time.sleep(45)
            try:
                for c0 in list(fleet.consoles):
                    b = fleet.bridge(c0["id"])
                    if b is None or not b.is_ps4():
                        continue
                    if time.time() - last.get(c0["id"], 0) < REPAIR_EVERY:
                        continue
                    # TWO REASONS TO STEP IN, and the second one is new. An icon that will not
                    # open is the obvious one. An icon that opens but is the WRONG BUILD is the
                    # one that kept this console on yesterday's app: everything the watchdog
                    # looked at said "fine", and the console cannot replace it by itself.
                    ok = b.ps4_tile_launchable()
                    stale = b.ps4_tile_stale()
                    why = ("will not open" if ok is False
                           else ("is an older build than the console's own copy of the shop"
                                 if stale is True else ""))
                    if not why:                    # fine, or could not ask - leave it alone
                        continue
                    last[c0["id"]] = time.time()
                    print(" %s: the dashboard icon %s - installing it from here, which is the one "
                          "lane the console's download service accepts" % (b.name, why))
                    try:
                        good, info = b.install_ps4_tile(force=True)
                        print(" %s: icon repair %s (%s)" % (b.name, "started" if good else "failed",
                                                            info))
                    except Exception as e:
                        print(" %s: icon repair failed: %s" % (b.name, e))
            except Exception:
                pass

    threading.Thread(target=tile_watchdog, daemon=True).start()

    transfers = {}
    queue = Queue(fleet, transfers, cfg)
    queue.library = library          # [B7] so the worker can verify the local PKG before handoff
    library.queue = queue            # and back, so renaming never touches a file a live job holds
    engine = source_engine.SourceEngine(assemble_sources(cfg))

    # The single-instance guard now runs at the top of main(), before anything has side effects.
    httpd = CompanionServer((cfg["companion"]["host"], cfg["companion"]["port"]), Handler)
    httpd.cfg, httpd.library, httpd.fleet, httpd.queue = cfg, library, fleet, queue
    # note_content_change() bumps library_gen, and the console tracker is handed the fleet rather
    # than the server - so the library hangs off the fleet as well. One attribute, set once.
    fleet.library = library
    httpd.transfers, httpd.engine, httpd.hashes = transfers, engine, load_hashes()
    httpd.dns = dns
    httpd.peers = PeerRegistry(cfg)

    sweep_temp_dbs(cfg.get("maintenance", {}).get("temp_sweep_age_sec", 3600))   # [B4] clean leftover temps
    start_library_watch(httpd)      # [B4] auto-rescan on library changes (debounced)
    start_cheat_sync_thread(httpd)  # ships the cheat library to a console that is missing it
    start_console_tracker(httpd)    # follows a console that changes address, by its durable id
    start_console_autostart(httpd)  # a PS4 that is switched on gets its shop back by itself
    # KEEP THIS APP CURRENT WITHOUT ANYBODY GOING TO LOOK. A PC only: a console has no exe to
    # replace. It installs the new build and then says so - it does not close a window somebody is
    # using (updates.auto_restart is how you ask for that instead).
    start_self_update_watch(httpd)

    start_ps5_log_listener(port=cfg.get("console", {}).get("log_port", 9097))
    start_pc_register_thread(cfg, fleet)   # announce our address to the on-console shop server
    # Find and keep the other PCs on our own: first sweep in the background (startup must not wait
    # on a /24 scan), then keep them fresh. Without this, PC-to-PC only worked while the console was
    # up to introduce them — which also made moving a game between PCs depend on the PS5 being on,
    # and it should not.
    httpd.peers.keepalive(httpd)

    ip, port = lan_ip(), cfg["companion"]["port"]
    print("=" * 64)
    print(" PKG MUTANT SHOP  companion  v%s" % VERSION)
    _dev = device_identity(cfg)
    print(" UI:        http://localhost:%d   (LAN http://%s:%d)" % (port, ip, port))
    print(" Phone/tablet: open  http://%s:%d  on any device on this network" % (ip, port))
    print(" This PC:   %s   id=%s" % (_dev.get("name"), _dev.get("id")))
    print(" Config:    %s" % CONFIG_PATH)
    print(" Consoles:  %s" % (", ".join("%s@%s" % (c["name"], c["ip"]) for c in fleet.consoles) or "(none set)"))
    print(" Parallel:  %d worker(s), per-console-parallel=%s" %
          (queue.max_parallel, queue.allow_ppc))
    print(" Library:   %d title(s)%s" % (len(library.games), " [empty — set library.local_paths]" if library.is_empty else ""))
    print("=" * 64)
    # Pop the shop UI in the default browser so it's obvious the app started.
    try:
        import webbrowser
        threading.Thread(target=lambda: (time.sleep(1.2),
                         webbrowser.open("http://localhost:%d/" % port)), daemon=True).start()
    except Exception:
        pass
    # The cheat library the exe carries, expanded beside it once. On its own thread and after the
    # server is built, because a PS4 asking for cheats is minutes away at the earliest and nothing
    # about starting up should wait on six thousand small file writes. From source this returns
    # immediately - there is no archive and the repo's own folder is already the library.
    threading.Thread(target=_unpack_cheat_library, daemon=True).start()
    # Serve in the background; a system-tray icon fronts it (hidden app, no console window).
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    if not run_tray(port):
        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("\nbye")


if __name__ == "__main__":
    main()

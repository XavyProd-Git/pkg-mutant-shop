# -*- coding: utf-8 -*-
"""Payloads & Homebrews: the catalogue, the two send lanes, and state nobody has to take on trust.

WHAT THIS IS. The owner keeps a folder of third-party payload ELFs and homebrew packages. This
module turns that folder into something the panel can show and press, on either console, and it is
deliberately small: every hard part already exists somewhere in this app and is called, not
reimplemented.

  * the catalogue is built at BUILD time by tools/gen_payload_catalog.py into
    web/assets/payloads-catalog.json, which both ELFs embed and the exe carries, so the panel works
    on a console with every PC switched off;
  * a payload reaches a PS5 through Payload Manager, exactly the way our own installer does, and a
    PS4 through GoldHEN's loader, exactly the way our own shop does;
  * a homebrew package is installed by THE SAME install engine the games use - it is handed to the
    queue as an ordinary install against a key this module registers for serving, which is how the
    PS4's home-screen package has always been shipped (PS4_TILE_KEY);
  * "is it running" is a port that answered, never an assumption, and a payload with nothing
    listening says so rather than showing a light nobody can back.

WHAT THIS MODULE MUST NEVER DO, each learned the hard way somewhere else in this repo:

  * never add the source folder to library.local_paths. Library.scan() registers every .pkg it
    finds as a game and normalise_pkg_names() RENAMES files on disk whose stem has characters
    outside [A-Za-z0-9._-] - it would rewrite the owner's
    "PS4-Xplorer 2.0 (LAPY20009) - 2.08.pkg" and put five homebrews on their game shelf;
  * never TCP-probe a PS4's :9090. Opening GoldHEN's payload port and closing it again stops it
    listening. The POST of an ELF is the probe;
  * never resolve a PS5 payload load by bare basename. Payload Manager resolves /loadpayload by
    BASENAME against its own registered directory, so a file uploaded anywhere else can silently
    run an older copy. Everything here writes to /data/pldmgr/payloads/<stem>/<file>, which is the
    convention companion/deploy.py already uses for our own ELF;
  * never start a jailbreak-layer payload as a side effect. payload_bundle.h states the rule in its
    own words and this module enforces it with `layer == "jailbreak"` needing an explicit confirm.
"""
import hashlib
import io
import json
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

# Where the console keeps what we give it. Both consoles already own SHOP_DATA_DIR and both already
# write into it at boot, so this is a subfolder of a folder that exists, not a new place on disk.
CONSOLE_PAYLOAD_DIR = "/data/pkg-mutant-shop/payloads"
CONSOLE_HOMEBREW_DIR = "/data/pkg-mutant-shop/homebrews"
# Payload Manager's OWN directory. /loadpayload resolves by basename against this, so a payload has
# to be here to be the one that actually runs. See companion/deploy.py's header.
PLDMGR_DIR = "/data/pldmgr/payloads"
# GoldHEN's own payload folder on a PS4 - a flat directory of .elf files, which is what its
# payload menu lists. Our send lane POSTs to the loader on :9090 and writes no file, so until now
# nothing this app did ever put our ELF where that menu could see it.
GOLDHEN_PAYLOAD_DIR = "/data/payloads"

CATALOG_NAME = "payloads-catalog.json"
DEFAULT_SRC = os.path.join("C:" + os.sep, "Mutant Payloads & HomeBrews")

# The key prefix under which a homebrew is registered for serving. It is NOT a library title: the
# registry entry is added after `games` is assembled, the way PS4_TILE_KEY is, so nothing about it
# can reach the owner's game shelf.
SERVE_PREFIX = "PMS-HOMEBREW/"

_cat_lock = threading.Lock()
_cat = {"path": None, "mtime": 0.0, "data": None}

_rel_lock = threading.Lock()
_rel_cache = {}                    # repo -> {"at": ts, "tag": str, "assets": [...], "error": str}
RELEASE_TTL_S = 6 * 3600           # a release check is not urgent; six hours is plenty


# --------------------------------------------------------------------------- #
# the catalogue                                                                #
# --------------------------------------------------------------------------- #
def catalog_path(web_dir):
    return os.path.join(web_dir, "assets", CATALOG_NAME)


def catalog(web_dir):
    """The shipped catalogue, re-read only when the file changes.

    Returns {} rather than raising when it is missing: a build without one must show an empty panel
    and keep the rest of the app working, not fail a request the header polls.
    """
    p = catalog_path(web_dir)
    try:
        mt = os.path.getmtime(p)
    except OSError:
        return {}
    with _cat_lock:
        if _cat["path"] == p and _cat["mtime"] == mt and _cat["data"] is not None:
            return _cat["data"]
    try:
        with io.open(p, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return {}
    with _cat_lock:
        _cat.update({"path": p, "mtime": mt, "data": data})
    return data


# THE SHAPE scan() WALKS, WRITTEN DOWN ONCE. scan() iterates
# (("payload", "Payloads"), ("homebrew", "Homebrews")) x PLATFORMS, so these four directories are
# exactly the places it will ever look. Keeping the list here rather than in the maker means the
# folders that get created and the folders that get read cannot drift apart - and test_payloads.py
# derives one from the other rather than hard-coding either.
SOURCE_LAYOUT = tuple(os.path.join(top, plat)
                      for top in ("Payloads", "Homebrews")
                      for plat in ("PS4", "PS5"))


def ensure_tree(root, subdirs):
    """Create `root` and each of `subdirs` inside it. Returns {ok, root, created, error?}.

    A MISSING FOLDER IS NOT A CONDITION TO REPORT, IT IS ONE TO FIX. The app used to tell the owner
    "That folder is not on this PC: C:/Mutant Payloads & HomeBrews" and leave them to go and make
    it - on a machine where the app knows the path, knows the layout, and had just decided it could
    do nothing without it. The games side has had a button for this since early on; the only thing
    a button adds over doing it is a chance to not press it.

    Deliberately forgiving: it creates what is missing and says so, and a root that cannot be made
    (a drive that is not there, a path with no permission) is an error returned, never an
    exception - this runs on the startup path and from inside a request, and neither may die
    because a folder could not be made.
    """
    created, base = [], os.path.normpath(root or "")
    if not base:
        return {"ok": False, "error": "no folder to create", "created": [], "root": base}
    for p in [base] + [os.path.join(base, d) for d in (subdirs or ())]:
        try:
            if not os.path.isdir(p):
                os.makedirs(p)
                created.append(p)
        except OSError as e:
            return {"ok": False, "error": str(e), "created": created, "root": base}
    return {"ok": True, "root": base, "created": created}


def ensure_source_tree(cfg):
    """Make sure the payloads/homebrews folder exists, with the four directories scan() reads."""
    return ensure_tree(source_root(cfg), SOURCE_LAYOUT)


def source_root(cfg):
    """Where the owner's folder is. Configurable, because a second PC will not have the same drive."""
    try:
        v = ((cfg or {}).get("payloads") or {}).get("root")
    except Exception:
        v = None
    return os.path.normpath(v) if v else DEFAULT_SRC


def bundled_ours(item):
    """Our own artifact as the running exe carries it, or None.

    THE EXE SHIPS THE PS4 PAYLOAD. server.py has bundled `ps4-elf/PKG-MUTANT-SHOP-PS4.elf` since
    long before this panel existed, for the reason written there: it is the difference between
    "switch the PS4 on" and "switch the PS4 on, then go and find the icon". So a second PC with no
    source folder was reporting "not on this PC" about a file it was carrying inside itself - the
    owner saw exactly that on Casita. The PS5's 34 MB ELF is deliberately NOT bundled, so this
    answers None for it and the peer lane takes over.

    Read from the frozen bundle only. From source the ordinary folder lookup already finds the repo
    copy, and pointing at ../ps5-app/onconsole here would make a dev checkout claim to hold a build
    it may not have made yet.
    """
    if not getattr(sys, "frozen", False):
        return None
    if (item or {}).get("file") != "PKG-MUTANT-SHOP-PS4.elf":
        return None
    p = os.path.join(getattr(sys, "_MEIPASS", ""), "ps4-elf", "PKG-MUTANT-SHOP-PS4.elf")
    return p if p and os.path.exists(p) else None


def kept_match(kept, item):
    """The path of this homebrew's package ON THE CONSOLE, out of the console's own kept list, or "".

    `kept` is {byte count: path} - the console reports every package it can reach and the owner
    renames files, so a byte count is the honest primary key: it is a fact about the contents.

    IT IS NOT ENOUGH ON ITS OWN. The size being compared is `item["size"]`, and for any item this
    machine does not hold that is the size the CATALOGUE recorded - the author's folder, for the
    release they had. DolphinPS4 is recorded at 202,244,096 bytes for v03.51 while the owner's
    console holds v04.00, so the console's copy was invisible to a panel that had its path in
    hand. The title id is what survives a version change, and the console reports it inside the
    path it gives us.
    """
    if not kept:
        return ""
    try:
        sz = int((item or {}).get("size") or 0)
    except (TypeError, ValueError):
        sz = 0
    if sz and sz in kept:
        return kept[sz] or ""
    tid = str((item or {}).get("title_id") or "").strip().upper()
    if not tid:
        return ""
    for p in kept.values():
        if tid in str(p or "").upper():
            return p or ""
    return ""


def register_local(registry, sizes, cfg, item):
    """Teach a serve registry about an item whose bytes are on THIS machine. Returns the key or "".

    WHY THIS EXISTS. The registry is built inside the library scan, and a scan is a moment:
    everything this panel downloads arrives after it, and nothing in the download lane triggers
    another. So the install route read the registry as if it were the filesystem and refused with
    "X is not on this PC" about a file it had just written there - which is what the owner hit,
    on a package they had downloaded seconds earlier.

    An empty answer means the bytes genuinely are not here, which is a different lane's problem
    (a peer may hold them, or they can be fetched). Nothing is invented: the path is only
    registered when os.path.exists agrees.
    """
    p = local_path(cfg, item)
    if not p:
        return ""
    key = serve_key(item)
    if not key:
        return ""
    registry[key] = p
    if sizes is not None:
        try:
            # A FOLDER APP HAS NO st_size WORTH HAVING - its size is the tree's, which the
            # catalogue already measured.
            sizes[key] = (item.get("size") or 0) if os.path.isdir(p) else os.path.getsize(p)
        except OSError:
            pass
    return key


def local_path(cfg, item):
    """Absolute path of an item's bytes on THIS PC, or None when the folder is not here.

    None is an ordinary answer, not an error: the exe runs on machines that never had the source
    folder, and the panel says "not on this PC" instead of pretending the file is reachable.
    """
    rel = (item or {}).get("path")
    if not rel:
        return None
    p = os.path.join(source_root(cfg), rel.replace("/", os.sep))
    if os.path.exists(p):
        return p
    return bundled_ours(item) if (item or {}).get("ours") else None


def serve_key(item):
    """The /library/<key> a homebrew is served under.

    IT HAS TO BE CLEAN, AND THAT IS NOT TIDINESS - IT IS THE WHOLE INSTALL.

    This used to be the file's path inside the owner's folder, verbatim. The console is handed that
    as a URL and gives it to its own installer, and **BGFT cannot fetch a URL with spaces or
    brackets in it**. Measured on the PS4, in its own install log:

        install: register failed rc=0x80991400 id=ED1633-PKGI13337_00-...
                 uri=http://10.0.0.76:8710/library/PMS-HOMEBREW/Homebrews/PS4/PKGI PS4/FPKGi_...pkg

    One space, in a folder the owner named "PKGI PS4". Itemzflow installed perfectly from the next
    folder along because that one happens to be called "Itemzflow". That is the entire difference
    between the homebrew that worked and the ones that did not, and it caught FPKGi on both
    consoles, PS4-Xplorer (spaces AND brackets) and the PS5's Internet Browser.

    Percent-escaping does not help - the PS5's own local lane says so from its own measurements:
    "It cannot fetch a percent-escaped URL ... the same request with a clean name returns res:0,
    the escaped one 'install failed'." That lane solves it by serving through a token url. This is
    the same answer for the lane where the PC does the serving.

    GAMES NEVER HIT THIS because the library renames them on disk - normalise_pkg_names() rewrites
    any stem with characters outside [A-Za-z0-9._-]. Homebrews are deliberately NOT in
    library.local_paths (that renamer would rewrite the owner's own files), so nothing was cleaning
    their names and nothing was meant to: the fix belongs here, on the way out, not on their disk.

    Built from the title id and the platform because both are stable, so a package that is renamed
    or moved inside the owner's folder keeps the same URL. It ends in .pkg because a url that does
    not is refused outright with 0x80990033.
    """
    plat = str((item or {}).get("platform") or "").upper()
    ident = str((item or {}).get("id") or "")
    key = re.sub(r"[^A-Za-z0-9._-]+", "-", "%s-%s" % (plat or "ANY", ident or "item")).strip("-")
    # Only a package gets the extension. A folder app is never fetched over HTTP by the console -
    # it is pushed into the drive ShadowMountPlus watches - so calling its key ".pkg" would be a
    # label that lies, and the install lane keys its routing off what the path actually is.
    return SERVE_PREFIX + key + (".pkg" if (item or {}).get("shape") == "pkg" else "")


def items_for(web_dir, platform=None, kind=None):
    out = []
    for it in (catalog(web_dir).get("items") or []):
        if platform and str(it.get("platform", "")).upper() != str(platform).upper():
            continue
        if kind and it.get("kind") != kind:
            continue
        out.append(it)
    return out


# --------------------------------------------------------------------------- #
# state: what is observably true right now                                      #
# --------------------------------------------------------------------------- #

def proc_stem(name):
    """The comparable stem of a payload filename.

    Payload Manager reports the name the payload was BUILT as, which is not always the name the
    file has on disk: measured on this console, "ftpsrv-ps5.elf" runs as "ftpsrv.elf" and
    "pldmgr_v0.5.2.elf" as "pldmgr.elf". Comparing raw filenames therefore reports two live
    payloads as stopped. Strip the extension, the platform suffix and a version suffix, and the two
    names meet in the middle.
    """
    n = (name or "").strip().lower()
    if n.endswith(".elf"):
        n = n[:-4]
    n = re.sub(r"[-_](ps4|ps5)$", "", n)
    n = re.sub(r"[-_]v?\d+(\.\d+)*[a-z0-9]*$", "", n)
    return n

def port_open(ip, port, timeout=0.9):
    if not ip or not port:
        return False
    s = socket.socket()
    s.settimeout(timeout)
    try:
        s.connect((ip, int(port)))
        return True
    except Exception:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def live_ports(ip, ports, timeout=0.9):
    """Probe several ports at once and return the set that answered.

    Threads because the panel asks about up to eight payloads and a console's accept loop should be
    touched briefly, once, rather than eight times in series while somebody watches a spinner.

    :9090 IS NOT PROBEABLE and is never in `ports` - see the module header.
    """
    ports = sorted({int(p) for p in ports if p})
    if not ip or not ports:
        return set()
    out, lock = set(), threading.Lock()

    def one(p):
        if port_open(ip, p, timeout):
            with lock:
                out.add(p)

    ts = [threading.Thread(target=one, args=(p,), daemon=True) for p in ports]
    for t in ts:
        t.start()
    for t in ts:
        t.join(timeout + 0.4)
    return out


# --------------------------------------------------------------------------- #
# sending a payload                                                             #
# --------------------------------------------------------------------------- #
def ps5_payload_dest(item):
    """Where a payload has to live for Payload Manager to actually run THAT copy."""
    fname = item.get("file") or os.path.basename(item.get("path") or "")
    stem = re.sub(r"\.elf$", "", fname, flags=re.I) or "payload"
    return "%s/%s/%s" % (PLDMGR_DIR, stem, fname)


class bridge_ip_shim(object):
    """console_load() wants something with an .ip; send_ps4 is handed the address itself."""
    def __init__(self, ip):
        self.ip = ip


def console_payload_sizes(ip, timeout=6.0):
    """{stem: size} for the payloads a console holds in PB_DIR, or None if it could not say.

    PB_DIR is the folder /api/payloads/load runs from, so this is the only set of sizes worth
    comparing against before deciding a send can be skipped. By stem, because the console carries
    each payload under the stable catalogue id while this PC has whatever the owner's file is
    called.
    """
    if not ip:
        return None
    try:
        with urllib.request.urlopen("http://%s:8710/api/payloads" % ip, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    have = ((j.get("state") or {}).get("have"))
    if have is None:
        return None
    out = {}
    for e in have:
        try:
            out[proc_stem(e.get("n"))] = int(e.get("s") or 0)
        except Exception:
            continue
    return out


def console_copy_is_current(bridge, cfg, item):
    """Does the copy the console would RUN match the one on this PC?

    AFTER AN UPDATE IT DOES NOT, AND THAT IS THE WHOLE POINT. Both ELFs write the payloads they
    carry to PB_DIR at boot, so "start the console's own copy" is normally free and correct - but
    the moment the update button replaces a file here, the console is still holding the previous
    build, and starting it would silently run the old version while the panel showed the new number.

    IT HAS TO BE THE COPY THAT WOULD ACTUALLY RUN. Comparing against the file we last uploaded to
    Payload Manager's folder answers a different question: /api/payloads/load resolves by stem
    against PB_DIR, so PB_DIR is what decides. Getting that wrong meant the first press sent the new
    bytes and the second happily started the old ones.

    Unknown - the console could not be asked, or this PC has no copy to compare - counts as current:
    refusing to start something on a doubt is worse than starting the copy that is there.
    """
    src = local_path(cfg, item)
    if not src:
        return True
    try:
        want = os.path.getsize(src)
    except OSError:
        return True
    st = console_state(getattr(bridge, "ip", ""))
    sizes = (st or {}).get("have")
    if sizes is None:
        return True
    got = sizes.get(proc_stem(os.path.basename(src)))
    if got is None:
        return False          # the console does not carry it at all - send it
    if not size_can_vouch(item):
        return False          # our own build: same length, different bytes - see size_can_vouch
    return got == want


def size_can_vouch(item):
    """Is "same number of bytes" good enough to call a console's copy current?

    NOT FOR OUR OWN ARTIFACTS, AND THE NUMBERS SAY SO. A build of this app differs from the last
    one by a version string of identical width, so the length does not move:

        PKG-MUTANT-SHOP-PS4.elf   3.89.0 / 3.89.1 / 3.90.0   all 9,522,528 bytes
        PKG-MUTANT-SHOP.elf       3.89.0 and 3.90.0          both 45,367,592 bytes

    Three releases, one length. Every size shortcut in this module therefore reports our freshly
    updated payload as already on the console and skips the send - and console_copy_is_current()
    then says the console's own copy is current, so the Run shortcut starts the OLD build. The
    docstring there says that check exists precisely to stop "starting it would silently run the
    old version while the panel showed the new number"; for the one payload we actually publish,
    it could not do that job.

    Somebody else's payload is a different matter: those carry their version in the FILENAME, so a
    new release is a new name and a different length, and skipping a 34 MB upload that is already
    there is worth having. This narrows the shortcut rather than removing it.
    """
    return not (item or {}).get("ours")


def console_load(bridge, item, timeout=60):
    """Ask the console to start a payload out of the copy ITS OWN ELF carries.

    Both ELFs write the bundled payloads to /data/pkg-mutant-shop/payloads at boot, so on any
    console running 3.84.0 or newer this is both faster than uploading two megabytes again and the
    only thing that works from a PC that has never had the owner's folder. Returns True only when
    the console says it started it; anything else falls back to the upload path, so an older payload
    on the console behaves exactly as it did before this existed.
    """
    fname = item.get("file") or ""
    ip = getattr(bridge, "ip", "")
    if not (ip and fname):
        return False
    try:
        with urllib.request.urlopen(
                "http://%s:8710/api/payloads/load?name=%s"
                % (ip, urllib.parse.quote(fname)), timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
        return bool(j.get("ok"))
    except Exception:
        return False


def send_ps5(bridge, cfg, item, log=None):
    """Upload a payload to the PS5 and ask Payload Manager to run it.

    Returns (ok, sentence). The sentence is what the owner reads, so it says what happened rather
    than what was attempted.
    """
    say = log or (lambda m: None)
    src = local_path(cfg, item)
    if not src:
        # NOT ON THIS PC IS NOT THE END OF IT. The console carries this payload too, so ask it to
        # start its own copy rather than refusing - which is what makes the panel work from a PC
        # that has never had the owner's folder.
        if console_load(bridge, item):
            return True, "Started %s from the console's own copy." % (item.get("title") or item.get("id"))
        return False, "That file is not on this PC, and the console does not carry it either."
    dest = ps5_payload_dest(item)
    size = os.path.getsize(src)
    sent = False
    if size_can_vouch(item) and console_file_size(bridge, dest) == size:
        say("[payloads] %s is already on the console - not sending it again" % dest)
    else:
        sent = True
        try:
            with io.open(src, "rb") as f:
                ok = bridge.fs_write(dest, f, size=size, timeout=600)
        except Exception as e:
            return False, "The console did not take the file (%s)." % e.__class__.__name__
        if not ok:
            return False, "The console did not take the file."
        say("[payloads] uploaded %s (%d bytes)" % (dest, size))
        # ...AND INTO THE CONSOLE'S OWN FOLDER, under the name it carries this payload as. PB_DIR is
        # what /api/payloads/load runs from and what console_copy_is_current() compares against, so
        # without this the console keeps answering with the build its ELF shipped and every press
        # would send the same bytes again for ever. It is overwritten from the ELF at the next boot,
        # which is correct: the embedded copy is only refreshed by a rebuild, and
        # sync_payload_bins --check is what makes sure that build happens.
        mirror = "%s/%s" % (CONSOLE_PAYLOAD_DIR, proc_stem(os.path.basename(src)) + ".elf")
        try:
            with io.open(src, "rb") as f:
                if bridge.fs_write(mirror, f, size=size, timeout=600):
                    say("[payloads] refreshed the console's own copy at %s" % mirror)
        except Exception:
            pass          # best effort: the load below uses the path we already wrote
        # OUR OWN ELF GETS PAYLOAD MANAGER'S METADATA TOO. `dest` above is already
        # /data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf for our build, so the bytes
        # have landed in the right folder since this lane was written - but a console that has
        # never had the payload has no <file>.json beside it, and that is what Payload Manager's
        # own upload writes and what makes the folder appear in its list.
        if manager_dest(item):
            _pldmgr_sidecar(bridge, dest, item, say)

    # A PROPERTY, NOT A METHOD. Ps5Bridge.pldmgr is @property and calling it raised
    # "'PayloadManager' object is not callable" AFTER the upload had already succeeded - the file
    # landed and the press still reported failure. getattr keeps working if it ever becomes a
    # method again.
    pm = bridge.pldmgr
    if callable(pm):
        pm = pm()
    if not pm:
        return False, "Payload Manager is not answering, so nothing can be started."
    if not pm.load(dest):
        return False, "Payload Manager did not take it."
    # SAY WHICH OF THE TWO THINGS HAPPENED. "Sent" when nothing was sent is a small lie that makes
    # the owner think a copy went over the network every time they press Run.
    title = item.get("title") or item.get("id")
    return True, (("Sent %s to the PS5." % title) if sent
                  else ("Started %s on the PS5 - it was already there." % title))


def send_ps4(ip, cfg, item, log=None, timeout=120, bridge=None):
    """Hand a payload to GoldHEN's loader on a PS4.

    THE POST IS THE PROBE. There is no connect-first check anywhere in this function, deliberately:
    opening :9090 and closing it again stops the loader listening, which is why the rest of this
    app refuses to touch that port any other way.
    """
    say = log or (lambda m: None)
    src = local_path(cfg, item)
    if not src:
        if console_load(bridge_ip_shim(ip), item):
            return True, "Started %s from the console's own copy." % (item.get("title") or item.get("id"))
        return False, "That file is not on this PC, and the console does not carry it either."
    try:
        body = io.open(src, "rb").read()
    except Exception:
        return False, "That file could not be read."
    req = urllib.request.Request("http://%s:9090/" % ip, data=body,
                                 headers={"Content-Type": "application/octet-stream"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
    except Exception as e:
        return False, "The payload loader did not take it (%s)." % e.__class__.__name__
    say("[payloads] posted %s to %s:9090" % (item.get("file"), ip))
    # ...AND LEAVE A COPY WHERE GOLDHEN'S MENU LOOKS. The POST above hands the bytes straight to
    # the loader, which runs them and keeps nothing: switch the console off and our payload is
    # gone again. GoldHEN reads a flat /data/payloads, so writing it there is what makes it
    # startable from the console on its own afterwards.
    #
    # Over the file API on :8710, never :9090. That port is the loader and this app's standing
    # rule is that the POST is the only thing allowed to touch it - opening it any other way
    # stops it listening.
    extra = ""
    if bridge is not None and manager_dest(item):
        iok, iwhere = install_ours(bridge, cfg, item, log=say, src=src)
        if iok and iwhere:
            extra = " It is in the PS4's payload folder too."
        elif not iok:
            extra = " It is running, but the copy for the payload menu %s." % iwhere
    return True, ("Sent %s to the PS4.%s" % ((item.get("title") or item.get("id")), extra))


# --------------------------------------------------------------------------- #
# seeding a homebrew onto the console, so no PC is needed afterwards            #
# --------------------------------------------------------------------------- #
def manager_dest(item):
    """Where this console's payload manager will find our ELF. "" for anything that is not ours.

    Only our own two artifacts, which is what OURS lists and what the owner asked for. Every other
    payload keeps going exactly where it goes today.
    """
    if not (item or {}).get("ours"):
        return ""
    fname = item.get("file") or os.path.basename(item.get("path") or "")
    if not fname:
        return ""
    if str(item.get("platform") or "").upper() == "PS4":
        return "%s/%s" % (GOLDHEN_PAYLOAD_DIR, fname)      # GoldHEN reads a flat folder
    return ps5_payload_dest(item)                          # already /data/pldmgr/payloads/<stem>/<file>


def _pldmgr_sidecar(bridge, dest, item, say):
    """Payload Manager's own metadata file, written only when there is not one already.

    Its web upload writes <file>.json beside the ELF. A console that has never had this payload
    has no such file, and writing one is what makes Payload Manager list the folder rather than
    ignore it. A sidecar that already exists belongs to Payload Manager - it may carry a source,
    a category or a checksum the owner set - so it is left exactly as it is.
    """
    side = dest + ".json"
    try:
        if console_file_size(bridge, side) is not None:
            return                                          # theirs; do not touch it
    except Exception:
        return
    body = json.dumps({
        "name": os.path.basename(dest),
        "filename": os.path.basename(dest),
        "url": "", "source": "", "source_direct": "", "description": "",
        "last_update": "", "version": str(item.get("version") or ""),
        "checksum": "", "category": "",
        "downloaded_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "install_source": "pkg-mutant-shop", "install_source_detail": "", "source_name": "",
    }, indent=2).encode("utf-8")
    try:
        if bridge.fs_write(side, body, size=len(body), timeout=30):
            say("[payloads] wrote %s" % side)
    except Exception:
        pass                                                # metadata; never worth a failure


def install_ours(bridge, cfg, item, log=None, src=None):
    """Copy our own ELF into the console's payload-manager folder.

    Returns (ok, sentence). `ok` is False only when there was something to do and it did not
    happen - "this is not our ELF" and "there is no console here" are both (True, "").

    BEST EFFORT, ON PURPOSE. The lanes that call this - download, update, send, install - must
    keep working with every console switched off, which is how they work today. A console that is
    asleep, or has no room, is reported in the sentence and changes nothing else.
    """
    say = log or (lambda m: None)
    dest = manager_dest(item)
    if not dest or bridge is None:
        return True, ""
    src = src or local_path(cfg, item)
    if not src or not os.path.isfile(src):
        return True, ""
    try:
        size = os.path.getsize(src)
    except OSError:
        return True, ""
    # THE LENGTH CANNOT VOUCH FOR OUR OWN BUILD, AND THIS IS THE ONE PLACE THAT ONLY EVER HANDLES
    # OUR OWN BUILD. size_can_vouch() exists for exactly these two filenames and says why in
    # numbers: PKG-MUTANT-SHOP-PS4.elf was 9,522,528 bytes across 3.89.0, 3.89.1 AND 3.90.0, and
    # PKG-MUTANT-SHOP.elf was 45,367,592 across two releases - a version string of fixed width
    # does not move the length. The first version of this function compared the size alone, which
    # is the precise failure that function was written to prevent: the lane would log "already
    # current", leave the previous build in the payload manager's folder, and nothing would ever
    # refresh it, because no boot path mirrors these two paths.
    #
    # So the shortcut is asked for properly and, for ours, always declines - which is what
    # send_ps5 above already does with the same call on the same files. The cost is re-writing a
    # file that may not have changed; the alternative is a console quietly running last week's
    # build with the panel showing this week's number.
    try:
        if size_can_vouch(item) and console_file_size(bridge, dest) == size:
            say("[payloads] %s is already current" % dest)
            return True, ""
    except Exception:
        pass
    try:
        with io.open(src, "rb") as f:
            ok = bridge.fs_write(dest, f, size=size, timeout=900)
    except Exception as e:
        return False, "could not be copied to %s (%s)" % (dest, e.__class__.__name__)
    if not ok:
        return False, "could not be copied to %s" % dest
    say("[payloads] installed %s (%d bytes)" % (dest, size))
    if dest.startswith(PLDMGR_DIR + "/"):
        _pldmgr_sidecar(bridge, dest, item, say)
    return True, dest


def console_homebrew_path(item):
    fname = item.get("file") or os.path.basename(item.get("path") or "")
    return "%s/%s" % (CONSOLE_HOMEBREW_DIR, fname)


def seed_homebrew(bridge, cfg, item, log=None, progress=None):
    """Copy a homebrew package onto the console's own disk, once.

    This is what makes requirement 7 true in the only way the arithmetic allows: the packages are
    264 MB and cannot ride inside an ELF that is loaded into RAM, but they can sit in the console's
    own data folder for ever. Once seeded, the console installs them with every PC switched off,
    because pkgfile_path_allowed() already accepts /data/...*.pkg and serves it to the installer
    over loopback.
    """
    say = log or (lambda m: None)
    src = local_path(cfg, item)
    if not src:
        return False, "That file is not on this PC."
    dest = console_homebrew_path(item)
    size = os.path.getsize(src)
    if size_can_vouch(item) and console_file_size(bridge, dest) == size:
        return True, "Already on the console."
    try:
        with io.open(src, "rb") as f:
            ok = bridge.fs_write(dest, f, size=size, timeout=3600)
    except Exception as e:
        return False, "The copy did not finish (%s)." % e.__class__.__name__
    if not ok:
        return False, "The copy did not finish."
    say("[payloads] seeded %s (%d bytes)" % (dest, size))
    return True, "Copied to the console."


# --------------------------------------------------------------------------- #
# upstream releases                                                             #
# --------------------------------------------------------------------------- #
def gh_token(cfg):
    """A GitHub token, if the owner has given us one. "" is the normal answer.

    IT IS ONLY EVER NEEDED FOR A REPOSITORY THAT IS NOT PUBLIC, which right now is ours: the app's
    own releases are private while they are being prepared, and api.github.com answers 404 to an
    unauthenticated request for a private repository - indistinguishable from "this project has no
    releases". Every third-party upstream in curated.json is public and needs none of this.

    Read from config (`updates.github_token`) or the environment, never written anywhere, never
    logged, and never sent to any host but api.github.com / objects.githubusercontent.com.
    """
    t = ""
    try:
        t = str(((cfg or {}).get("updates") or {}).get("github_token") or "").strip()
    except Exception:
        t = ""
    return t or str(os.environ.get("PMS_GITHUB_TOKEN") or "").strip()


def github_latest(repo, timeout=8.0, force=False, token=""):
    """The newest release of a GitHub project, cached.

    THIS IS THE ONLY THING IN THIS APP THAT TALKS TO THE INTERNET. Everything else speaks to the
    LAN. So it is written to fail quietly and completely: no retry, one short timeout, and every
    failure returns a dict with "error" set rather than raising into a request the panel polls.
    An owner who blocks Sony does not necessarily allow GitHub, and a firewall that drops this must
    cost one greyed line in a panel, never a broken app.
    """
    repo = (repo or "").strip().strip("/")
    if not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", repo):
        return {"error": "no upstream"}
    now = time.time()
    # THE CACHE KEY CARRIES WHETHER WE WERE AUTHENTICATED. Without that, one unauthenticated 404 on
    # our own private repo would be served back for five minutes to a caller that now HAS a token.
    ckey = repo + ("|auth" if token else "")
    with _rel_lock:
        c = _rel_cache.get(ckey)
        if c and not force and (now - c.get("at", 0)) < RELEASE_TTL_S:
            return c
    out = {"at": now, "repo": repo}
    hdrs = {"Accept": "application/vnd.github+json",
            # GitHub refuses a request with no User-Agent.
            "User-Agent": "PKG-MUTANT-SHOP"}
    if token:
        hdrs["Authorization"] = "Bearer " + token
    # NOT /releases/latest. GITHUB DEFINES THAT ENDPOINT AS "THE NEWEST RELEASE THAT IS NOT A
    # PRE-RELEASE", and the projects this panel tracks ship pre-releases as a matter of course.
    # Measured 2026-10-02: ShadowMountPlus had 1.7beta3 published while /releases/latest still
    # answered 1.7beta2, so the panel reported everything up to date and the owner could see the
    # newer build on the project's own page. That is the whole of "it is not picking up the latest
    # releases". The list endpoint returns newest-first including pre-releases, so take the first
    # one that is not a draft - a draft is not published and nobody can download it.
    req = urllib.request.Request(
        "https://api.github.com/repos/%s/releases?per_page=10" % repo, headers=hdrs)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            doc = json.loads(r.read().decode("utf-8", "replace"))
        rels = [x for x in doc if isinstance(x, dict) and not x.get("draft")] if isinstance(doc, list) else []
        if not rels:
            # A repo can have tags and no releases at all (Itemzflow does). Say so plainly rather
            # than reporting an empty version that reads as "up to date".
            out["error"] = "no releases"
            raise _NoRelease()
        j = rels[0]
        out["prerelease"] = bool(j.get("prerelease"))
        # What the project itself calls stable, when it differs - so the panel can say which.
        _stable = next((x for x in rels if not x.get("prerelease")), None)
        out["stable_tag"] = str((_stable or {}).get("tag_name") or "").strip()
        out["tag"] = str(j.get("tag_name") or j.get("name") or "").strip()
        out["url"] = str(j.get("html_url") or "")
        out["published"] = str(j.get("published_at") or "")[:10]
        # `api_url` IS NOT THE SAME LINK AND IS NEEDED FOR A PRIVATE RELEASE. browser_download_url
        # is only fetchable without credentials on a public repo; an asset of a private release is
        # fetched from the API url with the token and Accept: application/octet-stream. Both are
        # recorded so download_asset can choose without asking GitHub twice.
        out["assets"] = [{"name": a.get("name"), "size": a.get("size"),
                          "url": a.get("browser_download_url"), "api_url": a.get("url")}
                         for a in (j.get("assets") or []) if a.get("name")]
    except _NoRelease:
        pass
    except urllib.error.HTTPError as e:
        # THE REASON MATTERS, AND "HTTPError" IS NOT ONE. Unauthenticated api.github.com allows 60
        # requests an hour from one address, and a panel that checks ten projects burns that in six
        # presses - so a rate limit is the ordinary failure here, not an exotic one, and it must not
        # read the same as "this project has no releases". 404 is the other common answer and means
        # the repo or its releases are not there, which is a wrong entry in curated.json, not a
        # network problem.
        if e.code == 403 or e.code == 429:
            out["error"] = "rate-limited"
            out["retry_after"] = str(e.headers.get("x-ratelimit-reset") or "")
        elif e.code == 404:
            # 404 MEANS TWO DIFFERENT THINGS AND ONLY A TOKEN TELLS THEM APART. Authenticated, it
            # is definitive: the repo is there and has published nothing. Unauthenticated against a
            # PRIVATE repo - which ours is while it is being prepared - GitHub returns the same 404
            # it returns for a repo that does not exist, deliberately, so that a private name
            # cannot be probed. Reporting "no releases" in that case would be a claim we cannot
            # back, and it would go on being reported after the first release was published.
            out["error"] = "no releases" if token else "not visible"
        else:
            out["error"] = "HTTP %d" % e.code
    except Exception as e:
        out["error"] = e.__class__.__name__
    with _rel_lock:
        # A FAILURE IS CACHED TOO, BRIEFLY. Without this, a panel that polls while GitHub is
        # refusing would keep asking and keep being refused for the rest of the hour.
        if out.get("error"):
            out["at"] = now - RELEASE_TTL_S + 300      # try again in five minutes, not six hours
        _rel_cache[ckey] = out
    return out


class _NoRelease(Exception):
    """This repo publishes no releases at all - not a network failure, and not an update."""


def version_tuple(s):
    return tuple(int(x) for x in re.findall(r"\d+", str(s or ""))[:4])


def newer_than(tag, have):
    """Is `tag` a higher version than `have`? Unknown answers False, never True.

    A release check that guesses "newer" pushes somebody to replace a working payload for nothing.
    """
    a, b = version_tuple(tag), version_tuple(have)
    if not a or not b:
        return False
    return a > b


# --------------------------------------------------------------------------- #
# the folder scan - shared with tools/gen_payload_catalog.py                    #
# --------------------------------------------------------------------------- #
# OUR OWN ARTIFACTS, BY NAME. The owner keeps a copy of the shop in that folder, which is useful to
# them and must never become an .incbin: an artifact carrying a copy of itself is the recursion
# ps4-app/build-all-wsl.sh exists to prevent. Catalogued and marked, never embedded.
OURS = ("PKG-MUTANT-SHOP.elf", "PKG-MUTANT-SHOP-PS4.elf")
# OUR OWN VERSION, READ THE ONE WAY THAT WORKS FOR OUR OWN ARTIFACT.
#
# identify_elf() cannot be used on these and it is worth writing down why, because the reason is
# the whole shape of this app: the PS5 ELF EMBEDS ftpsrv, nanodns, kstuff, OnionHEN, Payload
# Manager and the WebKit autoloader, so every one of those projects' markers is inside it. The
# generic identifier requires exactly one marker hit and would therefore find six - and answer
# "unknown", correctly, for ever. That is also why `ours` items skipped identification entirely
# and consequently showed no version at all, which the owner noticed.
#
# So it is read by the one string only our own build writes: the web bundle's APP_VERSION. That
# line is generated from the single source of the version at build time, appears in both ELFs, and
# the other five-digit-looking numbers in the binary (a 3.84.0 quoted inside a code comment) do not
# match it. Nothing is guessed here: either that exact line is present or the version is unknown.
OURS_VER_RE = b'var APP_VERSION="([0-9][0-9.]*)"'


def ours_version(path):
    """The version of one of OUR OWN ELFs, out of the binary. "" when the line is not there."""
    try:
        with io.open(path, "rb") as f:
            blob = f.read()
    except Exception:
        return ""
    m = re.search(OURS_VER_RE, blob)
    try:
        return m.group(1).decode("ascii", "replace") if m else ""
    except Exception:
        return ""
PLATFORMS = ("PS4", "PS5")

def slug(s):
    s = re.sub(r"[^A-Za-z0-9]+", "-", (s or "").strip().lower())
    return re.sub(r"-+", "-", s).strip("-")


def sha256(path):
    h = hashlib.sha256()
    with io.open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def version_from_name(name):
    """The version a filename admits to, or "".

    Only Payload Manager writes a version into its own binary; everything else here carries it in
    the filename if at all. Reading it from the name and SAYING that is where it came from is
    honest; inventing one is not.
    """
    m = re.search(r"[_\- ]v?(\d+\.\d+(?:\.\d+)?(?:[a-z]+\d*)?)(?=[_\-. ]|$)", name, re.I)
    return m.group(1) if m else ""


def param_json_in(path, limit=2 << 20):
    """A PS5 param.json embedded near the front of a file, or None.

    InternetBrowser-PS5M.pkg is not a package this app's parser reads - it begins \\x7fFIH and holds
    a real package further in - but it carries a plain param.json in its header region, and that is
    where its real name and content id come from. Reading it is how the tile shows "Internet
    Browser" instead of a filename.
    """
    try:
        with io.open(path, "rb") as f:
            head = f.read(limit)
    except Exception:
        return None
    i = head.find(b'"contentId"')
    if i < 0:
        return None
    start = head.rfind(b"{", 0, i)
    while start >= 0:
        depth, j, ins, esc = 0, start, False, False
        while j < len(head):
            c = head[j:j + 1]
            if ins:
                if esc:
                    esc = False
                elif c == b"\\":
                    esc = True
                elif c == b'"':
                    ins = False
            elif c == b'"':
                ins = True
            elif c == b"{":
                depth += 1
            elif c == b"}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(head[start:j + 1].decode("utf-8", "replace"))
                    except Exception:
                        obj = None
                    # IT PARSED IS NOT THE SAME AS IT IS THE RIGHT ONE. The nearest "{" before the
                    # key is usually a SIBLING that closed before it - here it is "ageLevel": {...},
                    # which is perfectly good JSON - and returning that silently loses the title and
                    # the content id. Keep walking outwards until the object actually holds the key
                    # we came looking for.
                    if isinstance(obj, dict) and "contentId" in obj:
                        return obj
                    break
            j += 1
        start = head.rfind(b"{", 0, start)
    return None


def identify_elf(path, curated_payloads):
    """Which project this ELF is, read out of the FILE rather than its name.

    THE OWNER RENAMES THINGS, AND SAID SO. A payload called anything at all is still ftpsrv if the
    ftpsrv banner is inside it, and it keeps its port, its upstream and its warnings. Matching on
    the filename would lose every one of those the moment somebody tidied a folder.

    Returns (id or None, version or "", where the version came from). A marker that matches more
    than one curated project identifies nothing - that is a marker that needs to be better, and
    saying "unknown" is the honest answer until it is.
    """
    try:
        with io.open(path, "rb") as f:
            blob = f.read()
    except Exception:
        return None, "", ""
    hits = []
    for pid, c in (curated_payloads or {}).items():
        mark = c.get("marker")
        if mark and mark.encode("utf-8", "replace") in blob:
            hits.append(pid)
    if len(hits) != 1:
        return None, "", ""
    pid = hits[0]
    rx = (curated_payloads.get(pid) or {}).get("ver_re")
    if rx:
        m = re.search(rx.encode("utf-8", "replace"), blob)
        if m:
            try:
                return pid, m.group(1).decode("ascii", "replace"), "file"
            except Exception:
                pass
    return pid, "", ""


def read_pkg(path):
    """Title/id/version out of a package, using the app's OWN parser.

    The library and this panel must agree about what a package is, so this calls the same
    companion/pkg_meta.py the library calls rather than a second implementation that could drift.
    """
    try:
        import pkg_meta
    except Exception:
        return None
    try:
        return pkg_meta.parse_pkg(path)
    except Exception:
        return None


def folder_app(d):
    """A PS5 app folder (eboot.bin + sce_sys/param.json), or None.

    This is the RetroArch shape. It is not a package and must never be sent down the package lane;
    it goes where a game backup goes.
    """
    pj = os.path.join(d, "sce_sys", "param.json")
    if not (os.path.isfile(pj) and os.path.isfile(os.path.join(d, "eboot.bin"))):
        return None
    try:
        with io.open(pj, encoding="utf-8", errors="replace") as f:
            return json.load(f)
    except Exception:
        return None


_dirstat_memo = {}


def dir_stats(d):
    """Files and bytes under a folder-shaped app, remembered between scans.

    RetroArch alone is 3,000 files. Walking it on every live rescan - which now happens while the
    panel is open - would make the panel the most expensive thing in the app. The folder's own mtime
    changes when anything is added or removed at its top level, and for a shipped app that is the
    only thing that ever changes, so it is a sound key. A cold cache costs exactly one walk.
    """
    try:
        key = (d, os.path.getmtime(d))
    except OSError:
        return 0, 0
    hit = _dirstat_memo.get(key)
    if hit:
        return hit
    n = 0
    total = 0
    for dp, _dn, fn in os.walk(d):
        for f in fn:
            try:
                total += os.path.getsize(os.path.join(dp, f))
                n += 1
            except OSError:
                pass
    if len(_dirstat_memo) > 64:
        _dirstat_memo.clear()
    _dirstat_memo[key] = (n, total)
    return n, total


def scan(src, curated=None):
    items = []
    for kind, top in (("payload", "Payloads"), ("homebrew", "Homebrews")):
        for plat in PLATFORMS:
            base = os.path.join(src, top, plat)
            if not os.path.isdir(base):
                continue
            for group in sorted(os.listdir(base)):
                gdir = os.path.join(base, group)
                if not os.path.isdir(gdir):
                    continue
                items.extend(scan_group(kind, plat, group, gdir, src, curated))
    items.sort(key=lambda it: (it["kind"], it["platform"], it["id"]))
    return items


def scan_group(kind, plat, group, gdir, src, curated=None):
    out = []
    gslug = slug(group)
    for name in sorted(os.listdir(gdir)):
        p = os.path.join(gdir, name)

        if os.path.isdir(p):
            pj = folder_app(p)
            if pj and kind == "homebrew":
                nfiles, total = dir_stats(p)
                tid = (pj.get("titleId") or name or "").strip()
                loc = (pj.get("localizedParameters") or {})
                title = ((loc.get(loc.get("defaultLanguage") or "en-US") or {}).get("titleName")
                         or group)
                # THE RELEASE, IF WE PUT IT THERE. param.json's version belongs to the app and
                # never moves; the release tag is the thing the update lane can compare. When both
                # are known the release wins and `version_from` says so, which is what stops a
                # just-downloaded emulator reporting an update to itself.
                rtag = read_release_note(p)
                pver = pj.get("masterVersion") or pj.get("contentVersion") or ""
                out.append({
                    "id": tid or gslug, "kind": kind, "platform": plat, "group": group,
                    "shape": "folder", "path": os.path.relpath(p, src).replace("\\", "/"),
                    "title": title, "title_id": tid,
                    "content_id": pj.get("contentId") or "",
                    "version": rtag or pver,
                    # "release" when we wrote it down, "param" when it is the app's own number -
                    # which cannot be lined up against a release tag at all.
                    "version_from": "release" if rtag else ("param" if pver else ""),
                    "app_version": pver,
                    "files": nfiles, "size": total,
                })
            continue

        low = name.lower()
        if kind == "homebrew" and low.endswith(".elf"):
            # A HELPER, NOT A HOMEBREW. Two of the emulators do not start without one of these
            # running, which their own curated note says outright, and the download lane has been
            # putting them here all along - where nothing could launch them, because this branch
            # used to accept an .elf only while walking Payloads/. An emulator the app installed
            # and then could not start is the whole bug.
            #
            # It is a payload, so it is emitted as one: Send and Run already work for a payload on
            # both consoles and neither needed a line changed for this.
            # THE VERSION COMES OFF THE NAME. The tile prints it in its own column, and an id
            # built from a stem that carries it becomes a DIFFERENT id the day the helper is
            # updated - so the same helper would arrive as a brand new tile instead of the same
            # one holding a newer file. A dot is required in the number, which is what keeps
            # "PS5X360" and "helper" intact.
            stem = re.sub(r"[-_ ]v?\d+(?:\.\d+)+[A-Za-z0-9.\-]*$", "", name[:-4]) or name[:-4]
            _hb = (curated or {}).get("homebrews", {}) or {}
            _c = _hb.get(gslug) or {}
            if not _c:
                for _k2, _c2 in _hb.items():
                    if slug(str((_c2 or {}).get("title") or "")) == gslug:
                        _c = _c2 or {}
                        break
            # WHAT IT IS FOR, IN THE WORDS ALREADY WRITTEN. Each curated extra carries a `why`;
            # matched to the file by the asset-name prefix the download lane used to fetch it.
            _why = ""
            for _ex in (_c.get("extras") or []):
                _a = str((_ex or {}).get("asset") or "")
                if _a and low.startswith(_a.lower()):
                    _why = str(_ex.get("why") or "")
                    break
            _hver = version_from_name(name)
            out.append({
                # Unique and stable: two apps may both ship a file called helper.elf.
                "id": "%s-%s" % (gslug, slug(stem)),
                "kind": "payload", "platform": plat, "group": group,
                "shape": "elf", "path": os.path.relpath(p, src).replace("\\", "/"),
                "file": name,
                # Which app it belongs to, said on the tile, because "helper" on its own names
                # nothing.
                "title": "%s \u00b7 %s" % (group, stem),
                "blurb": _why,
                "helper_for": str(_c.get("title_id") or "") or gslug,
                "version": _hver, "version_from": "name" if _hver else "",
                "identified_by": "folder",
                "size": os.path.getsize(p), "sha256": sha256(p),
                "ours": False,
            })
            continue

        if kind == "payload" and low.endswith(".elf"):
            # CONTENT FIRST, FOLDER SECOND. The marker inside the binary decides what this is; the
            # folder name is only the fallback for something we have never seen before.
            det, dver, dfrom = (None, "", "")
            if name in OURS:
                # Identified by being one of our own filenames - see OURS_VER_RE above for why the
                # content identifier cannot be asked about a binary that carries six other projects.
                dver = ours_version(p)
                if dver:
                    dfrom = "file"
            else:
                det, dver, dfrom = identify_elf(p, (curated or {}).get("payloads"))
            ver = dver or version_from_name(name)
            out.append({
                "id": det or gslug, "kind": kind, "platform": plat, "group": group,
                "shape": "elf", "path": os.path.relpath(p, src).replace("\\", "/"),
                "file": name, "title": group, "version": ver,
                "version_from": (dfrom or ("name" if ver else "")),
                "identified_by": "content" if det else "folder",
                "size": os.path.getsize(p), "sha256": sha256(p),
                "ours": name in OURS,
            })
        elif kind == "homebrew" and low.endswith(".zip"):
            # A ZIP IS HOW MOST OF THE EMULATORS SHIP. Nothing here read one, so a download that
            # had plainly arrived - the file sitting in the folder it was put in - was invisible
            # to the catalogue, and its tile went on offering the download for ever.
            # It is not opened: what is inside is the console's business, and reading a 700 MB
            # archive on every folder scan to learn something the catalogue already states would
            # cost far more than it tells us. Identified by its folder, like a folder app.
            try:
                zsize = os.path.getsize(p)
            except OSError:
                continue
            # THE TITLE ID, WHEN THE NAME CARRIES IT. Several of these ship as PPSA50011.zip or
            # PPSA97358.zip - the console's own id for the app. That is what the panel matches
            # against the console's installed list, so picking it up here is the difference
            # between a tile that can say "Installed" and one that can only say it is on this PC.
            # Nothing is invented: no id in the name means no id, and the tile says less.
            ztid = ""
            zm = re.search(r"(?:PPSA|CUSA|PLAS|NPXS)\d{4,5}", name, re.I)
            if not zm:
                zm = re.search(r"(?:PPSA|CUSA|PLAS|NPXS)\d{4,5}", group, re.I)
            if zm:
                ztid = zm.group(0).upper()
            out.append({
                "id": gslug, "kind": kind, "platform": plat, "group": group,
                "shape": "zip", "path": os.path.relpath(p, src).replace("\\", "/"),
                "file": name, "size": zsize, "sha256": sha256(p),
                "title_id": ztid,
                "version": "", "version_from": "",
                "identified_by": "folder",
            })

        elif kind == "homebrew" and low.endswith(".pkg"):
            m = read_pkg(p) or {}
            pj = None if m else param_json_in(p)
            tid = (m.get("title_id") or "").strip()
            cid = (m.get("content_id") or "").strip()
            title = (m.get("title") or "").strip()
            unreadable = not m
            if pj:
                cid = cid or (pj.get("contentId") or "")
                loc = (pj.get("localizedParameters") or {})
                title = title or ((loc.get(loc.get("defaultLanguage") or "en-US") or {})
                                  .get("titleName") or "")
            if not tid and cid:
                mm = re.match(r"[A-Z0-9]{6}-([A-Z0-9]{9})_", cid)
                if mm:
                    tid = mm.group(1)
            out.append({
                "id": tid or gslug, "kind": kind, "platform": plat, "group": group,
                "shape": "pkg", "path": os.path.relpath(p, src).replace("\\", "/"),
                "file": name, "title": title or group, "title_id": tid, "content_id": cid,
                "version": (m.get("app_ver") or m.get("version") or version_from_name(name) or ""),
                "category": m.get("category") or "",
                "size": os.path.getsize(p), "sha256": sha256(p),
                # NOT A FAILURE, A FACT. The owner asked for this one to be listed and for the
                # console to decide, so it is carried with the flag set rather than dropped.
                "unreadable": unreadable,
            })
    return out


def apply_curated(items, curated):
    for it in items:
        section = "payloads" if it["kind"] == "payload" else "homebrews"
        table = curated.get(section, {})
        tid = str(it.get("title_id") or "").strip().upper()
        # WHICH KEY MATCHED, recorded on the item. There are four ways in and they have a
        # precedence; a test that re-derives them is a second copy of this rule that can disagree
        # with it, and the one gate that watches for an unreachable curated entry was exactly that.
        ckey = ""
        for cand in (it.get("title_id") or "", it["id"], slug(it.get("group", ""))):
            if cand and cand in table:
                ckey = cand
                break
        c = table.get(ckey) if ckey else None
        if not c and tid:
            for k2, c2 in table.items():
                if str((c2 or {}).get("title_id") or "").strip().upper() == tid:
                    ckey, c = k2, c2
                    break
        c = c or {}
        it["curated_key"] = ckey if c else ""
        # `marker` travels with the item because the update lane checks it INSIDE a download
        # before letting it replace a working payload.
        for k in ("title", "blurb", "port", "autostart", "layer", "repo", "asset", "ours",
                  "marker", "probe",
                  # Added for the catalogue of things that are NOT here yet. `ext` matters even
                  # for an installed one: pick_asset otherwise only ever accepts .pkg for a
                  # homebrew and .elf for a payload, and several of these ship a .zip.
                  "ext", "extras", "emulates", "fw", "homepage", "note", "source_only",
                  "catalog", "data_dirs"):
            if k in c and c[k] is not None and (k != "title" or c[k]):
                it[k] = c[k]
        it.setdefault("port", 0)
        it.setdefault("autostart", False)
        it.setdefault("repo", None)
        it["curated"] = bool(c)
        # Everything apply_curated() is handed came from a directory scan, so the file is here.
        # available_items() below produces the other kind.
        it.setdefault("have", True)
    return items


def already_here(c, key, items):
    """Is the thing this curated entry describes ALREADY in the folder, under any name?

    It very often is, under a name that shares nothing with the catalogue's. The owner's RetroArch
    sits in "RetroArch - Alpha 5 - PS5" and the entry for it is keyed `retroarch-ps5`: matching on
    the id alone offered a fresh 261 MB download of an emulator that was already installed and
    working, which is the one thing this list must never do.

    Three ways to recognise it, in order of how much they prove:
      * the console's own title id - PPSA99169 is PPSA99169 whoever named the folder;
      * the upstream it came from - two entries for one repo are the same program;
      * the id, which is what matched before and still catches the simple case.
    A match on any of them means the panel should be offering an UPDATE, not a download, and the
    update drawer is already looking at the present copy.
    """
    tid = str(c.get("title_id") or "").strip().upper()
    repo = str(c.get("repo") or "").strip().lower()
    ids = set()
    for i in items:
        ids.add(i.get("id"))
        if str(i.get("title_id") or "").strip().upper() == tid and tid:
            return True
        if repo and str(i.get("repo") or "").strip().lower() == repo:
            return True
    return key in ids or (slug(c.get("title") or key) or key) in ids


def available_items(curated, items):
    """Curated entries marked `catalog` that are NOT in the folder, as items the panel can show.

    THE CATALOGUE COULD ONLY EVER DESCRIBE A FILE THAT WAS ALREADY ON DISK. Every item came out of
    scan(), so the panel's answer to "what is there?" and its answer to "what exists?" were the
    same list - and an emulator nobody had downloaded yet could not be mentioned at all, let alone
    offered. These fill that gap: same shape as a scanned item, `have` false, and a `dest_dir`
    saying where the download belongs, because there is no existing path to derive it from.

    `items` is what the folder scan found, so nothing already here is listed as available - see
    already_here() for the three ways one of these is recognised under a different name.
    """
    out = []
    for section, kind in (("payloads", "payload"), ("homebrews", "homebrew")):
        for key, c in (curated.get(section) or {}).items():
            if not c.get("catalog"):
                continue
            if already_here(c, key, items):
                continue
            plat = str(c.get("platform") or "").upper()
            if plat not in PLATFORMS:
                continue                      # an entry with no platform cannot be placed
            title = c.get("title") or key
            top = "Payloads" if kind == "payload" else "Homebrews"
            # THE SAME ID THE SCAN WILL PRODUCE once this has been downloaded. The file lands in a
            # folder named after the title and scan_group() ids it by slug(folder), so deriving it
            # any other way means the downloaded copy and the offer never recognise each other -
            # the item would be listed twice, once as present and once as still available.
            ident = slug(title) or key
            it = {
                "id": ident, "kind": kind, "platform": plat, "group": title,
                "shape": "pkg" if str(c.get("ext") or "").lower() == ".pkg" else "zip",
                "path": "", "dest_dir": "%s/%s/%s" % (top, plat, title),
                "size": 0, "sha256": "", "version": "", "version_from": "",
                # AN INVENTED ROW, not a file. The grid hides these - there is nothing to
                # press - while a real entry with have:false is a genuine item this PC happens
                # not to hold, and its tile must still show.
                "have": False, "offer": True, "curated": True,
            }
            for k in ("title", "blurb", "repo", "asset", "ext", "extras", "emulates", "fw",
                      "homepage", "note", "source_only", "port", "autostart", "layer",
                      "marker", "probe", "catalog", "title_id", "data_dirs"):
                if k in c and c[k] is not None:
                    it[k] = c[k]
            it.setdefault("port", 0)
            it.setdefault("autostart", False)
            out.append(it)
    out.sort(key=lambda i: (i["kind"], i["platform"], str(i.get("title") or i["id"]).lower()))
    return out


def known_versions(root=None):
    """Versions this app has PROVEN, keyed by the file's own sha256.

    Some payloads carry no version anywhere - not in the binary, not in the filename - so the only
    honest answer would be a blank. But a file that is byte-for-byte the size of an asset in an
    upstream release IS that release, and the running app makes that comparison every time it
    checks for updates. What it learns is written to assets/payloads/known-versions.json and
    stamped in here, so the answer ships inside both ELFs and is there on a console with no PC and
    no internet. Keyed by sha256, so a different build of the same project never inherits it.
    """
    # A PACKAGED BUILD HAS NO assets/ BESIDE IT. __file__ is inside the one-file extraction, so the
    # path below resolves to somewhere that does not exist and this returned {} in every shipped
    # exe - which is how six payloads ended up with no version, and therefore no update, for ever.
    if root is None and getattr(sys, "frozen", False):
        pf = os.path.join(getattr(sys, "_MEIPASS", ""), "payload-versions", "known-versions.json")
        try:
            with io.open(pf, encoding="utf-8") as f:
                return (json.load(f).get("versions") or {})
        except Exception:
            pass
    here = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(here, "assets", "payloads", "known-versions.json")
    try:
        with io.open(p, encoding="utf-8") as f:
            return (json.load(f).get("versions") or {})
    except Exception:
        return {}


def build(src, curated, known=None):
    """The catalogue for a folder. `curated` is the hand-written table, passed in by the caller.

    It is passed in rather than read here because the two callers get it from different places: the
    build tool reads assets/payloads/curated.json, and the running app reads the copy the generator
    embeds in the catalogue itself - so a live rescan needs no file the exe does not carry.
    """
    if not os.path.isdir(src):
        raise SystemExit("source folder not found: %s" % src)
    items = apply_curated(scan(src, curated), curated)
    # ...and then everything the curated table says EXISTS but this folder does not hold. The panel
    # needs both to answer "what can I get?" without pretending the folder is the whole world.
    items.extend(available_items(curated, items))
    kv = known if known is not None else known_versions()
    for it in items:
        if it.get("version") or it.get("kind") != "payload":
            continue
        hit = kv.get(it.get("sha256") or "")
        if hit and hit.get("version"):
            it["version"] = hit["version"]
            it["version_from"] = "release"
    tot = {}
    for it in items:
        key = "%s_%s" % (it["kind"], it["platform"].lower())
        tot[key] = tot.get(key, 0) + 1
    return {
        # NO TIMESTAMP. A generated file that changes every run cannot be compared by --check, and
        # this project already has a gate that exists because a stale blob shipped unnoticed.
        "schema": 1,
        # THE CURATED TABLE TRAVELS WITH THE CATALOGUE. A live rescan - after the update button
        # replaces a payload, or the owner drops a file in while the panel is open - needs the
        # markers, ports and upstreams, and this way it needs no file the exe does not already ship.
        "curated": curated,
        "source_name": os.path.basename(src.rstrip("\\/")),
        "counts": tot,
        "items": items,
    }


# --------------------------------------------------------------------------- #
# the live view                                                                 #
# --------------------------------------------------------------------------- #
_live_lock = threading.Lock()
_live = {"sig": None, "cat": None}


def folder_sig(root):
    """A cheap fingerprint of the source folder: what is there, how big, how recently touched.

    DELIBERATELY SHALLOW. Payloads are walked fully - eleven files - but a homebrew is identified by
    its own entry rather than by everything inside it, because one of them is a 3,000-file app
    folder and this runs while somebody watches a panel. Adding, removing, renaming or replacing
    anything the catalogue can see changes this string; editing a texture three levels inside
    RetroArch does not, which is the right trade.
    """
    parts = []
    for top in ("Payloads", "Homebrews"):
        base = os.path.join(root, top)
        if not os.path.isdir(base):
            continue
        for plat in PLATFORMS:
            pdir = os.path.join(base, plat)
            if not os.path.isdir(pdir):
                continue
            try:
                groups = sorted(os.listdir(pdir))
            except OSError:
                continue
            for g in groups:
                gdir = os.path.join(pdir, g)
                try:
                    names = sorted(os.listdir(gdir))
                except OSError:
                    continue
                for n in names:
                    p = os.path.join(gdir, n)
                    try:
                        st = os.stat(p)
                    except OSError:
                        continue
                    parts.append("%s/%s/%s|%d|%d" % (plat, g, n, st.st_size, int(st.st_mtime)))
    return hashlib.sha256("\n".join(parts).encode("utf-8", "replace")).hexdigest()[:16]


def live_catalog(cfg, web_dir):
    """The catalogue as the folder is RIGHT NOW, or the shipped one when the folder is not here.

    WHY THIS EXISTS. The catalogue is generated at build time so a console with no PC still has one.
    But the panel also has to notice a payload the update button just replaced, and a file the owner
    dropped in while looking at it - neither of which a baked file can show. So the PC rescans when
    the folder's fingerprint changes and serves that; a PC without the folder, and both consoles,
    keep using the shipped copy.

    The curated table comes from the shipped catalogue itself, so this needs no file the exe does
    not already carry.
    """
    baked = catalog(web_dir)
    root = source_root(cfg)
    if not os.path.isdir(root):
        # MAKE IT, THEN CARRY ON. This is the moment the app discovers the folder is missing, so it
        # is the moment to create it rather than to report it. This module is Python and therefore
        # only ever runs on a PC companion - both consoles answer /api/payloads from their own C
        # server and the baked catalogue - so there is no device here that should be left without
        # the folder. If it still cannot be made (a drive that is not plugged in, a path with no
        # permission) ensure_tree returns that as a value and the baked catalogue is served, which
        # is exactly what happened before and is still the right fallback.
        ensure_tree(root, SOURCE_LAYOUT)
        if not os.path.isdir(root):
            return _own_have(baked, cfg), ""
    try:
        sig = folder_sig(root)
    except Exception:
        return _own_have(baked, cfg), ""
    with _live_lock:
        if _live["sig"] == sig and _live["cat"] is not None:
            return _live["cat"], sig
    try:
        cat = build(root, baked.get("curated") or {})
    # BaseException, not Exception: build() raises SystemExit as its ordinary "the folder is not
    # there any more" path, and SystemExit is not an Exception - so a drive unplugged between the
    # isdir() above and this call killed the request thread instead of falling back.
    except BaseException:
        return _own_have(baked, cfg), sig
    cat = _merged(baked, cat, cfg)
    with _live_lock:
        _live["sig"], _live["cat"] = sig, cat
    return cat, sig


def _own_have(baked, cfg):
    """A copy of the shipped catalogue whose `have` is about THIS machine.

    For the three ways out of live_catalog that never reach _merged: the folder could not be made,
    its signature could not be read, or the scan raised. Those are exactly the machines least
    likely to hold the files, and they were being served the author's `have` verbatim - so the
    Download drawer hid every item ("you already have this") while the tile refused to install it
    ("Not on this PC"). The owner saw that on three homebrews their console had installed.

    Copied, never written through: catalog() hands out one memoised object and the nested item
    dicts come with it.
    """
    if cfg is None:
        return baked
    out = []
    for it in (baked.get("items") or []):
        it = dict(it)
        if not it.get("offer"):
            it["have"] = local_path(cfg, it) is not None
        out.append(it)
    fixed = dict(baked)
    fixed["items"] = out
    return fixed


def _merged(baked, live, cfg=None):
    """Everything this build knows about, with the folder's own copy winning where there is one.

    THE PANEL IS NOT A DIRECTORY LISTING. It is the fleet's catalogue: eighteen things that exist,
    each annotated with where it can be had from - this PC, the console, or another PC. Returning
    only what is in this folder makes a second PC's panel collapse to whatever it happens to hold.

    That is not hypothetical, and the way it happened is worth writing down. A PC with no folder
    served the baked catalogue, so all eighteen tiles appeared and could be pressed. Then the folder
    started being created automatically, so an empty scan could happen - guarded by falling back to
    baked when the scan found NOTHING. The owner then took an update on that PC, which downloaded
    one file into the new folder. The scan was no longer empty, the guard no longer fired, and the
    whole panel became that single file: "Nothing here for this console" on the other tab, and no
    payloads or homebrews anywhere. One successful download emptied the shelf.

    An "or" between two catalogues was always the wrong shape. A folder that holds some of the
    items is the ORDINARY case - it is what every PC looks like between the first download and the
    last - so the two are merged: the live entry wherever the folder has the file (its real
    version, size and hash, which is what an update and a send need), the shipped entry everywhere
    else (so the tile is still there, and `here` can honestly say the bytes are elsewhere).

    Keyed on id + platform, which is what the rest of this module already treats as an item's
    identity; anything in the folder that the build has never heard of is kept as well.
    """
    def _same(it):
        """What makes two entries the same PROGRAM, whatever each is keyed by."""
        plat = str(it.get("platform") or "").upper()
        tid = str(it.get("title_id") or "").strip().upper()
        repo = str(it.get("repo") or "").strip().lower()
        out = set()
        if tid:
            out.add((plat, "tid", tid))
        if repo:
            out.add((plat, "repo", repo))
        return out

    # THE BAKED SIDE'S PROGRAMS, before anything is decided. A live OFFER for one of these is the
    # same program described twice - the offer by the project's name, the baked entry by the title
    # id inside the file - and only one of them may reach the panel.
    baked_real = {}
    for it in (baked.get("items") or []):
        if it.get("offer") or it.get("have") is False:
            continue
        for k in _same(it):
            baked_real[k] = it

    # WHICH SHIPPED ENTRIES A LIVE OFFER PROVED ARE NOT HERE. Carried as keys and applied to the
    # COPY further down, because `baked` is the memoised object catalog() hands to every caller:
    # writing "have" into it made the catalogue's own shape depend on how many times it had been
    # read (measured: 29 items, then 36, because the pass above skips entries whose have is False).
    no_bytes = set()
    out, seen = [], set()
    for it in (live.get("items") or []):
        if it.get("offer"):
            hit = None
            for k in _same(it):
                if k in baked_real:
                    hit = baked_real[k]
                    break
            if hit is not None:
                # Let the baked entry carry it - it knows the title id, the size and the version -
                # and remember what the offer knew: this PC does not hold the file. Remembered
                # rather than written, see no_bytes above.
                no_bytes.add((hit.get("id"), str(hit.get("platform") or "").upper()))
                continue
        out.append(it)
        seen.add((it.get("id"), str(it.get("platform") or "").upper()))
    # AN OFFER IS NOT A THING THAT EXISTS SOMEWHERE ELSE. Every other baked entry describes a file
    # that is real on some machine, which is the whole reason this merge keeps them. An entry with
    # `have` false describes a file that is real nowhere - it is this build's list of what COULD be
    # downloaded, and the live scan computes that same list from the same curated table, against
    # what is in the folder now.
    #
    # Keeping both is not a duplicate by id, which is why the key above did not catch it: a
    # downloaded emulator is identified by the title id inside it (PPSA50011) and the offer by the
    # slug of its name (ps5x360). Three emulators were downloaded, unpacked and installed, and the
    # drawer went on offering all three as if nothing had happened.
    # Safe unconditionally: this function is reached only with a catalogue that came out of
    # build(), and build() always runs available_items(). Every path where the scan could not be
    # done returns the baked catalogue whole, without coming through here - so "the live side
    # produced no offers" means everything is downloaded, not that nothing was computed. Gating on
    # "are there any" would have brought all of them back at exactly that moment.
    for it in (baked.get("items") or []):
        if (it.get("id"), str(it.get("platform") or "").upper()) in seen:
            continue
        # A STUB, not merely something this PC lacks. Those are two different things now: the
        # loop above can mark a real baked entry have:false to say "the bytes are not on this
        # machine", and that entry's tile must still be drawn. A stub is the row available_items
        # invents, which has no file anywhere - `offer`, or no path at all on an older catalogue
        # built before that flag existed.
        if it.get("offer") or (it.get("have") is False and not it.get("path")):
            continue
        # `have` IS A FACT ABOUT THIS MACHINE, AND IT WAS BEING INHERITED FROM THE AUTHOR'S.
        # The baked catalogue is generated by scanning the author's folder, so its `have` is true
        # for every file THEY hold. Shipped verbatim, a second PC reported "have: true" and
        # "from: peer" in the same breath - the file is here, and also the file is on the other
        # machine - and because the Download drawer keys "you already have this" on `have`, it
        # hid the item while the tile refused to install it. With the other PC switched off that
        # left a dead tile and no way to fetch what it was refusing: measured on three PS4
        # homebrews the console already had installed.
        #
        # The filesystem is asked instead, so this is right either way - the author's path often
        # does exist here as well, and then the drawer is correct to hide it. Four lines below,
        # the same reasoning already corrects `version` for the same reason.
        # A SHIPPED ENTRY DESCRIBES THE MACHINE THAT BUILT THE APP, NOT THIS ONE. The catalogue is
        # generated by scanning the author's folder at build time, so every field in it - including
        # `version` and `version_from: "file"` - is a fact about a folder on another computer at
        # another moment. Reported verbatim, a PC that does not hold the file says it read a
        # version out of a file it does not have.
        #
        # That is not cosmetic: it is why our own PS4 tile offered "3.89.0 -> v3.90.0" on a PC
        # already carrying 3.90.0. The shipped catalogue inside the 3.90.0 build genuinely records
        # 3.89.0, because that is what the author's folder held when 3.90.0 was built - a release
        # behind, by construction, for ever.
        #
        # Where the running exe CARRIES the file (our PS4 payload, see bundled_ours) the true
        # version is readable right here, and it is the version that would actually be sent.
        # Otherwise the entry keeps its shipped number and says so, so nothing claims to have read
        # a file that is not on this machine.
        it = dict(it)
        # ...AND THAT INCLUDES WHETHER THE FILE IS HERE. Done on the copy, which is the whole
        # reason the copy exists: the same line placed above it wrote into the memoised shipped
        # catalogue and left have=False on every item for the life of the process.
        if cfg is not None:
            it["have"] = local_path(cfg, it) is not None
        if (it.get("id"), str(it.get("platform") or "").upper()) in no_bytes:
            it["have"] = False
        _b = bundled_ours(it) if it.get("ours") else None
        _v = ours_version(_b) if _b else ""
        if _v:
            it["version"], it["version_from"] = _v, "bundled"
        elif it.get("version"):
            it["version_from"] = "shipped"
        out.append(it)
    merged = dict(baked)
    merged.update(live)
    merged["items"] = out
    return merged


# --------------------------------------------------------------------------- #
# updating a payload from its upstream release                                  #
# --------------------------------------------------------------------------- #
def running_exe():
    """The .exe this process is running from, or None when it is not a frozen build."""
    if not getattr(sys, "frozen", False):
        return None
    p = os.path.abspath(sys.executable or "")
    return p if p.lower().endswith(".exe") and os.path.isfile(p) else None


def sweep_old_exe():
    """Remove what an update left beside the app. Called once at startup; finishes in the background.

    TWO FILES, BOTH MEASURED ON THE OWNER'S PC after a self-update:

      <exe>.old   the build that was running. It cannot be deleted at the moment of the swap -
                  that is the file the process is executing from - so it is left for the next
                  start. But the next start is quick: this one waits only for the PORT to come
                  free, and Windows can still hold the image for a moment after that, so a single
                  attempt at startup loses the race and the 38 MB file simply stays. Measured
                  exactly that: the app restarted itself correctly and the .old was still there.

      <exe>.new   a download that did not finish. Nothing reads it, so it was invisible - and after
                  a restart interrupted one, 25 MB of a half-fetched exe sat next to the app for
                  ever.

    So both are swept, and the sweep retries for a minute in the background rather than giving up
    on the first refusal. It never touches anything else, and a failure is not worth a word to the
    owner - it is housekeeping, and the next start tries again.
    """
    cur = running_exe()
    if not cur:
        return

    def _try(left):
        for ending in list(left):
            p = cur + ending
            try:
                os.remove(p)
                print("[update] cleared %s" % os.path.basename(p))
                left.remove(ending)
            except OSError:
                pass              # still mapped; try again shortly
        return left

    # ONE ATTEMPT INLINE, which is all it usually takes, and only then a background retry. Doing it
    # all in a thread would make the ordinary case racy for anything that looks straight afterwards.
    left = _try([e for e in (".old", ".new") if os.path.exists(cur + e)])
    if not left:
        return

    def _go():
        for _ in range(30):
            time.sleep(2.0)
            if not _try(left):
                return

    threading.Thread(target=_go, daemon=True).start()


# Releases whose exe this process has already put in place. See update_self().
_swapped_to = set()
_swap_lock = threading.Lock()


def update_self(cfg, rel, log=None):
    """Replace this companion's own .exe with the one in `rel`. Returns (ok, sentence).

    WINDOWS WILL NOT LET A RUNNING .EXE BE OVERWRITTEN - AND WILL LET IT BE RENAMED. That single
    fact is the whole method, and it is why this is safe rather than clever:

        download -> <exe>.new        nothing live is touched
        <exe>    -> <exe>.old        allowed while running; this process keeps executing from it
        <exe>.new -> <exe>           the next launch is the new build
        <exe>.old                    deleted by sweep_old_exe() on that next start

    If the second rename fails the first is undone, so the worst outcome is the build that was
    already running, still running, under its own name.

    THIS IS WHY IT EXISTS. The panel could update every payload and every homebrew and the one
    thing it could not update was itself: pick_asset() only ever matches .elf, so a PC took the
    update, reported the new version for the console, and went on running the old companion. The
    owner hit that twice and reasonably read it as the update not having worked.
    """
    say = log or (lambda m: None)
    cur = running_exe()
    if not cur:
        return False, "This is not the packaged app, so there is nothing to replace."
    # ONCE PER RELEASE, NOT ONCE PER TILE. Our own entry appears twice in the catalogue - one per
    # console - and taking both, which "Update all" does in one go, called this twice. The second
    # call found the running image already renamed aside by the first and failed trying to move it
    # again, so a PC that had just updated itself perfectly reported "Could not set the running app
    # aside (PermissionError)". Measured on the owner's second PC: the PS5 row said it had updated
    # and the PS4 row, half a second later, said that.
    #
    # The swap is per RELEASE, so remembering the tag is the whole guard. The exe on disk cannot be
    # read back to answer this - it is a PyInstaller archive and its copy of the page is compressed,
    # which is why tools/release.py has to extract the bundle to read a version out of one.
    tag = str((rel or {}).get("tag") or "")
    with _swap_lock:
        if tag and tag in _swapped_to:
            return True, "Updated. Close the app and open it again to use the new version."
    asset = None
    for a in (rel.get("assets") or []):
        if str(a.get("name") or "").lower().endswith(".exe"):
            asset = a
            break
    if not asset:
        return False, "That release has no Windows app in it."

    url = asset.get("url")
    tok = gh_token(cfg)
    if tok and asset.get("api_url"):
        url = asset["api_url"]
    new, old = cur + ".new", cur + ".old"
    try:
        hdrs = {"User-Agent": "PKG-MUTANT-SHOP"}
        if tok and url == asset.get("api_url"):
            hdrs["Authorization"] = "Bearer " + tok
            hdrs["Accept"] = "application/octet-stream"
        req = urllib.request.Request(url, headers=hdrs)
        got = 0
        with urllib.request.urlopen(req, timeout=600) as r, io.open(new, "wb") as f:
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
    except Exception as e:
        try: os.remove(new)
        except OSError: pass
        return False, "The download did not finish (%s)." % e.__class__.__name__

    want = int(asset.get("size") or 0)
    if want and got != want:
        try: os.remove(new)
        except OSError: pass
        return False, "The download was %d bytes and should be %d." % (got, want)
    # IS IT A WINDOWS PROGRAM AT ALL? A redirect to an error page is the shape this has to refuse,
    # because the next step renames it over the app.
    try:
        with io.open(new, "rb") as f:
            magic = f.read(2)
    except Exception:
        magic = b""
    if magic != b"MZ":
        try: os.remove(new)
        except OSError: pass
        return False, "What came down is not a Windows program, so it was not used."

    try:
        if os.path.exists(old):
            os.remove(old)
    except OSError:
        pass
    try:
        os.replace(cur, old)
    except Exception as e:
        try: os.remove(new)
        except OSError: pass
        return False, "Could not set the running app aside (%s)." % e.__class__.__name__
    try:
        os.replace(new, cur)
    except Exception as e:
        try: os.replace(old, cur)        # put it back exactly as it was
        except OSError: pass
        return False, "Could not put the new app in place (%s)." % e.__class__.__name__
    with _swap_lock:
        if tag:
            _swapped_to.add(tag)
    say("[update] the app on disk is now the new build; the old one is %s" % os.path.basename(old))
    return True, "Updated. Close the app and open it again to use the new version."


def pick_asset(rel, item):
    """The one file in a release that would replace THIS item, or None.

    A release usually carries several things - the WebKit autoloader ships an installer ELF, a
    Python host and a Windows exe - and only one of them is the payload we hold. The curated
    `asset` substring narrows it, and the extension decides the rest: a payload is an .elf and a
    homebrew is a .pkg, so a release of host tools is correctly "no update for this".
    """
    # THE ITEM MAY SAY. A homebrew is a .pkg and a payload is a .elf often enough to be a good
    # default, but it is only a default: several of the emulators ship a .zip that is unpacked on
    # the console, and with the default alone pick_asset returned None for every one of them -
    # which the panel reports, correctly but uselessly, as "no file that could replace this one".
    want_ext = str(item.get("ext") or "").lower() or (
        ".pkg" if item.get("kind") == "homebrew" else ".elf")
    needle = str(item.get("asset") or "").lower()
    plat = str(item.get("platform") or "").lower()          # "ps4" / "ps5"
    other = "ps4" if plat == "ps5" else "ps5"
    cands = []
    for a in (rel.get("assets") or []):
        name = str(a.get("name") or "")
        low = name.lower()
        if not low.endswith(want_ext):
            continue
        if needle and needle not in low:
            continue
        cands.append((low, a))
    if not cands:
        return None
    # THE PLATFORM IS PART OF THE MATCH, and leaving it out was a real bug: ftpsrv publishes
    # ftpsrv-ps4.elf and ftpsrv-ps5.elf in one release, and picking "the shortest name that ends
    # .elf" offered the PS4 build as the PS5's update. An asset naming the OTHER console is never
    # a candidate; one naming this console wins; anything neutral is the fallback.
    named = [a for low, a in cands if plat and plat in low]
    if named:
        return min(named, key=lambda a: len(str(a.get("name") or "")))
    neutral = [a for low, a in cands if not (other and other in low)]
    if not neutral:
        return None
    return min(neutral, key=lambda a: len(str(a.get("name") or "")))


def download_asset(cfg, item, asset, log=None, timeout=180):  # noqa: C901
    """Fetch a release asset and put it where the old file was.

    Returns (ok, sentence, new_version).

    THREE THINGS THIS REFUSES TO DO, each of which would be worse than not updating:

      * overwrite before the download is complete - it writes <name>.part and only then moves it
        into place, so a dropped connection leaves the working payload untouched;
      * accept a file that is not the thing it replaces - the marker that identifies the project is
        checked INSIDE the downloaded bytes before anything is moved. A release whose asset names
        drifted, or a redirect to something else entirely, is refused rather than installed;
      * leave two copies behind. The new file usually has a new name (…_v0.5.1.elf beside
        …_v0.5.0.elf) and both would then be catalogued as the same project, so the old one is
        removed once the new one is in place - and kept as .bak until that moment.
    """
    say = log or (lambda m: None)
    # THE DESTINATION IS THE OWNER'S FOLDER, ALWAYS - NEVER WHEREVER THE OLD COPY HAPPENED TO BE.
    #
    # local_path() answers with the copy INSIDE THE RUNNING EXE when the folder has none: the exe
    # bundles PKG-MUTANT-SHOP-PS4.elf on purpose (bundled_ours), so a second PC can seed a PS4 that
    # has nothing. Taking that answer as the place to WRITE meant an update downloaded into
    # PyInstaller's own temp extraction directory. It then read the version back out of the file it
    # had just written and honestly reported success - and the bytes went with the process when it
    # exited. Nothing in the owner's folder changed, so the panel offered the same update again on
    # the next poll, for ever.
    #
    # Measured on the owner's second PC: POST /api/payloads/update for our PS4 entry answered
    # {"ok":true,"version":"3.90.0"} and the folder fingerprint did not move - byte for byte the
    # same sixteen-character hash before and after. PS4 only, because bundled_ours() matches no
    # other file, which is exactly why the PS5 half of the same tile updated and the PS4 half did
    # not. The owner reported it as "I installed the update and it shows the update again".
    #
    # So the folder is derived from the item's recorded path under source_root() and an existing
    # file is only treated as something to replace when it is actually THERE.
    rel = str((item or {}).get("path") or "").replace("/", os.sep)
    if rel:
        folder = os.path.dirname(os.path.join(source_root(cfg), rel))
    else:
        # NOTHING TO REPLACE, SO NOTHING TO DERIVE A FOLDER FROM. An item that is not in the folder
        # yet carries dest_dir instead - the place the layout says it belongs - and the folder is
        # made on the way rather than reported as missing.
        dd = str((item or {}).get("dest_dir") or "").replace("/", os.sep)
        if not dd:
            return False, "There is no recorded place for that file.", ""
        folder = os.path.join(source_root(cfg), dd)
        # The folder is created by the `if not src:` block below, which already had to do exactly
        # this for an update whose file had gone missing.
    src = local_path(cfg, item)
    if src and os.path.normcase(os.path.dirname(os.path.abspath(src))) != \
            os.path.normcase(os.path.abspath(folder)):
        src = None          # the copy we found is the bundled one; it is not ours to replace
    if not src:
        # NOT HAVING IT IS A REASON TO FETCH IT, NOT A REASON TO REFUSE. This used to answer "that
        # file is not on this PC, so there is nothing to replace", which is true and useless: the
        # owner pressed Update on a device that had just been told an update exists, and the app
        # knows the project, the release, the asset and exactly where the file belongs.
        try:
            if not os.path.isdir(folder):
                os.makedirs(folder)
        except OSError as e:
            return False, "Could not make a place to put it (%s)." % e.__class__.__name__, ""
        # EMPTY WHEN THERE IS NO OLD FILE AT ALL, and that distinction matters a few lines
        # down. `rel` is the item's recorded path; an item that has never been downloaded has
        # none, and os.path.basename("") is "" - so this produced src == folder, the DIRECTORY.
        # `had_old` then saw a thing that exists and the backup step renamed the folder aside,
        # taking the .part file inside it along, and the move failed with a bare OSError that
        # named the old file it was supposedly replacing. There was no old file.
        src = os.path.join(folder, os.path.basename(rel)) if rel else ""
        say("[payloads] %s is not in the folder yet - fetching it into %s" % (
            (item or {}).get("title") or "it", folder))
    # A PRIVATE RELEASE IS NOT FETCHED FROM THE BROWSER LINK. With a token we ask the API for the
    # asset itself, which is the only route that works while our own repository is private; without
    # one the public browser link is right and nothing changes for the third-party upstreams.
    tok = gh_token(cfg)
    url = (asset.get("api_url") if (tok and asset.get("api_url")) else asset.get("url"))
    if not url:
        return False, "That release has no download for this file.", ""
    newname = os.path.basename(str(asset.get("name") or "")) or os.path.basename(src)
    dest = os.path.join(folder, newname)
    part = dest + ".part"
    try:
        dh = {"User-Agent": "PKG-MUTANT-SHOP"}
        if tok and url == asset.get("api_url"):
            dh["Authorization"] = "Bearer " + tok
            dh["Accept"] = "application/octet-stream"
        req = urllib.request.Request(url, headers=dh)
        with urllib.request.urlopen(req, timeout=timeout) as r, io.open(part, "wb") as f:
            got = 0
            while True:
                chunk = r.read(1 << 18)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
    except Exception as e:
        try:
            os.remove(part)
        except OSError:
            pass
        return False, "The download did not finish (%s)." % e.__class__.__name__, ""

    want = int(asset.get("size") or 0)
    if want and got != want:
        try:
            os.remove(part)
        except OSError:
            pass
        return False, "The download was %d bytes and should be %d." % (got, want), ""

    # IS IT STILL THE SAME PROJECT? The marker is what identifies this payload no matter what the
    # file is called, so it is also the right thing to check before letting a download replace it.
    mark = (item.get("marker") or "")
    if mark:
        try:
            with io.open(part, "rb") as f:
                blob = f.read()
        except Exception:
            blob = b""
        if mark.encode("utf-8", "replace") not in blob:
            try:
                os.remove(part)
            except OSError:
                pass
            return False, "That download is not %s, so it was not used." % (
                item.get("title") or item.get("id")), ""

    bak = src + ".bak"
    # THERE MAY BE NOTHING TO BACK UP. Since an update can now also be a first download, `src` is
    # a file that does not exist yet - and os.replace() on a missing source raises, which would
    # have turned every first fetch into "could not replace the old file".
    # A FOLDER IS NOT AN OLD FILE. For an app folder the item's recorded path IS the folder, so
    # local_path() answers with a directory - and this backup step then tried to rename the whole
    # mounted app aside, which Windows refuses outright (PermissionError) and which would be wrong
    # even where it succeeded. What is downloaded for one of those is an ARCHIVE that lands beside
    # the folder; replacing the folder is unpack_zip's job, and it keeps the old copy until the new
    # one is in place exactly as this does for a file. Measured: re-taking XPSemu answered "could
    # not replace the old file" and changed nothing.
    had_old = os.path.isfile(src)
    try:
        if os.path.exists(bak):
            os.remove(bak)
        if had_old:
            os.replace(src, bak)      # the old one survives until the new one is in place
        os.replace(part, dest)
        if had_old:
            os.remove(bak)
    except Exception as e:
        # put it back exactly as it was
        try:
            if had_old and not os.path.exists(src) and os.path.exists(bak):
                os.replace(bak, src)
        except OSError:
            pass
        try:
            os.remove(part)
        except OSError:
            pass
        return False, "Could not replace the old file (%s)." % e.__class__.__name__, ""

    say("[payloads] %s %s (%d bytes)"
        % (("updated %s ->" % os.path.basename(src)) if src else "downloaded", newname, got))
    # THE VERSION WE JUST INSTALLED, out of the file where the name does not carry it. Our own
    # releases publish PKG-MUTANT-SHOP.elf with no version in the filename - deliberately, because
    # the name is a contract the update lane matches on - so version_from_name() answered "" and the
    # panel reported an update with no version at all. Our artifacts say it inside themselves.
    ver = version_from_name(newname)
    if not ver and (item or {}).get("ours"):
        ver = ours_version(dest)
    return True, "Updated to %s." % newname, ver


_kv_lock = threading.Lock()


# --------------------------------------------------------------------------- #
# archives                                                                     #
# --------------------------------------------------------------------------- #
# MOST OF THE EMULATORS SHIP A .ZIP, AND NOTHING ON EITHER CONSOLE CAN BE HANDED ONE.
# A package goes to the installer, an app folder goes to the folder ShadowMountPlus watches, and an
# archive goes nowhere at all: downloading one left a file sitting in a folder with every button
# that could act on it refusing, which reads exactly like a broken download.
#
# The upstreams say what is inside and where it belongs, in their own words:
#   PS5X360   "copy the complete PPSA50011 folder to /data/homebrew/PPSA50011"
#   PS5SX2    "ShadowMountPlus mounts PS5SX2 from /data/homebrew/PPSA99203/"
#             "put PCSX2 patch files in /data/PCSX2/patches/"
#   XPSemu    the archive is PPSA97358/, the same shape
# /data/homebrew is already where mount_dest_for_drive() puts a folder app, so once the archive is
# opened the existing lane does the rest and nobody touches an FTP client. The data trees beside the
# app (PS5SX2's PCSX2/) are the one thing that lane has no place for, so they are kept beside the
# app with a note saying where each one goes, and the install lane reads that note.

RELEASE_NOTE = "mutant-release.json"   # which release this app folder came out of
EXTRAS_DIR = "_extras"           # beside the app, never a folder app itself, so the scan skips it
EXTRAS_NOTE = "mutant-extras.json"
UNPACKED_SUFFIX = ".unpacked"    # the archive is KEPT, under a name no scanner branch matches


def _zip_safe(name):
    """Would extracting this member write outside the folder we chose? (zip-slip)

    Nothing downloaded from the internet gets to pick its own absolute path or walk up out of the
    directory it was given, however well-known the repository is.
    """
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    if ":" in name.split("/", 1)[0]:          # C:/... on Windows
        return False
    parts = name.replace("\\", "/").split("/")
    return ".." not in parts


def zip_plan(path, limit=60000):
    """What an archive holds, read from its central directory.

    The central directory is a few KB whatever the archive weighs, so this costs the same for
    Porpoise's 30 MB as it would for RetroArch's 261 MB: the member names and sizes, no inflation.

    Answers one of four kinds:
      app      an app folder (eboot.bin + sce_sys/param.json), possibly inside a wrapper directory,
               possibly with data trees beside it
      pkg      one or more packages, which the installer takes directly
      data     files but no app and no package - a data drop for something already installed
      unknown  nothing recognisable, which is reported rather than guessed at
    """
    import zipfile
    out = {"ok": False, "kind": "unknown", "why": "", "app_root": "", "tid": "",
           "pkgs": [], "data_roots": [], "entries": 0, "unsafe": []}
    try:
        with zipfile.ZipFile(path) as z:
            names = z.namelist()
            out["entries"] = len(names)
            if len(names) > limit:
                out["why"] = "the archive has %d entries" % len(names)
                return out
            out["unsafe"] = [n for n in names if not _zip_safe(n)][:5]
            if out["unsafe"]:
                out["why"] = "it wants to write outside the folder"
                return out
            files = set(n.replace("\\", "/") for n in names if not n.endswith("/"))
            dirs = set()
            for n in files:
                p = n.split("/")
                for i in range(1, len(p)):
                    dirs.add("/".join(p[:i]))

            # A PACKAGE WINS. It is the lane with the fewest unknowns: the console's own installer
            # reads it and decides, exactly as it does for a game.
            pkgs = sorted(n for n in files if n.lower().endswith(".pkg"))
            if pkgs:
                out.update({"ok": True, "kind": "pkg", "pkgs": pkgs})
                return out

            # AN APP FOLDER, at any depth: eboot.bin with a param.json beside it in sce_sys.
            roots = sorted(d for d in dirs
                           if (d + "/eboot.bin") in files and (d + "/sce_sys/param.json") in files)
            if not roots:
                # eboot.bin alone still identifies it; a missing param.json only costs us the title.
                roots = sorted(d for d in dirs if (d + "/eboot.bin") in files)
            if roots:
                app = roots[0]
                out["app_root"] = app
                tid = app.rsplit("/", 1)[-1]
                m = re.match(r"^(?:PPSA|CUSA|PLAS|NPXS)\d{4,5}$", tid, re.I)
                if m:
                    out["tid"] = tid.upper()
                else:
                    try:
                        with zipfile.ZipFile(path) as z2:
                            pj = json.loads(z2.read(app + "/sce_sys/param.json")
                                            .decode("utf-8", "replace"))
                        out["tid"] = str(pj.get("titleId") or "").strip().upper()
                    except Exception:
                        out["tid"] = ""
                # WHAT ELSE TRAVELLED WITH IT. PS5SX2 ships PCSX2/ beside PPSA99203/ inside one
                # wrapper directory; those are the console's data, not part of the app, and copying
                # them into the app folder would put PCSX2/ inside the mounted title.
                wrap = app.rsplit("/", 1)[0] if "/" in app else ""
                here = wrap + "/" if wrap else ""
                tops = set()
                for n in files:
                    if wrap and not n.startswith(here):
                        continue
                    rest = n[len(here):]
                    if "/" in rest:
                        tops.add(rest.split("/", 1)[0])
                out["data_roots"] = sorted(here + t for t in tops
                                           if (here + t) != app)
                out.update({"ok": True, "kind": "app"})
                return out

            if files:
                out.update({"ok": True, "kind": "data", "why": "no app and no package inside"})
                return out
            out["why"] = "the archive is empty"
            return out
    except Exception as e:
        out["why"] = "the archive could not be read (%s)" % e.__class__.__name__
        return out


def _replace_dir(final, staged):
    """Put `staged` where `final` is, keeping the old one until the new one is in place.

    rename() does not replace an existing directory on Windows any more than it replaces a file on
    the consoles, so the old copy is moved aside first and only deleted once the new one has
    landed. A failure part-way leaves the old copy recoverable instead of leaving nothing.
    """
    import shutil
    # The parent may not exist yet: the extras live one level down (_extras/PCSX2) and nothing has
    # made _extras before this. rename() into a missing directory is a FileNotFoundError, which read
    # as "the archive did not open" - a true statement about a destination problem.
    parent = os.path.dirname(final)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    old = ""
    if os.path.exists(final):
        for i in range(1, 50):
            cand = "%s.old-%d" % (final, i)
            if not os.path.exists(cand):
                old = cand
                break
        if not old:
            raise OSError("too many leftover copies of %s" % os.path.basename(final))
        os.rename(final, old)
    try:
        os.rename(staged, final)
    except OSError:
        if old:
            os.rename(old, final)        # put it back; nothing was lost
        raise
    if old:
        shutil.rmtree(old, ignore_errors=True)


def _replace_file(final, staged):
    """Same as _replace_dir, for one file."""
    old = ""
    if os.path.exists(final):
        old = final + ".old"
        if os.path.exists(old):
            os.remove(old)
        os.rename(final, old)
    try:
        os.rename(staged, final)
    except OSError:
        if old:
            os.rename(old, final)
        raise
    if old:
        try:
            os.remove(old)
        except OSError:
            pass


def write_release_note(folder, tag, repo, asset=""):
    """Record which release an app folder came out of, inside the folder.

    A FOLDER APP HAS NO VERSION A RELEASE TAG CAN BE COMPARED WITH. param.json says 01.00 and will
    say 01.00 for ever; the release says v2.5, Alpha-2, vk-285-130. Compared as numbers those read
    as an update that is permanently available - all three emulators reported one the moment they
    were downloaded, against the very release they had just come from.

    The archive's own sha256 cannot vouch for the folder either, which is how every other item
    proves its version, so the one honest answer is to write down what we know at the only moment
    anybody knows it: the download. A folder with no note is reported as not comparable, which is
    the truth for one the owner installed by hand.
    """
    try:
        with io.open(os.path.join(folder, RELEASE_NOTE), "w", encoding="utf-8") as f:
            f.write(json.dumps({"tag": tag or "", "repo": repo or "", "asset": asset or "",
                                "at": int(time.time())}, indent=1, sort_keys=True))
        return True
    except OSError:
        return False


def read_release_note(folder):
    """The tag written by write_release_note(), or ""."""
    try:
        with io.open(os.path.join(folder, RELEASE_NOTE), encoding="utf-8") as f:
            return str((json.load(f) or {}).get("tag") or "")
    except Exception:
        return ""


def unpack_zip(cfg, item, log=None, tag="", repo=""):
    """Open a downloaded archive into what the console lane actually takes.

    `tag` and `repo` are what the release resolution already knows and the folder cannot work out
    for itself; they are written inside the app folder so the update lane has something it can
    actually compare next time. Opening an archive nobody downloaded through us passes neither, and
    the folder then honestly reports no release.

    Returns (ok, message, info). The archive itself is kept, renamed so no scanner branch matches
    it again - this app does not delete the owner's downloads, and the renamed copy is also the
    record of what was opened.
    """
    import shutil
    import zipfile
    say = log or (lambda m: None)
    src = local_path(cfg, item)
    if not src or not os.path.isfile(src):
        return False, "That archive is not on this PC.", {}
    if not src.lower().endswith(".zip"):
        return False, "That is not an archive.", {}
    gdir = os.path.dirname(src)
    plan = zip_plan(src)
    if not plan.get("ok"):
        return False, "Nothing recognisable inside: %s." % (plan.get("why") or "unknown shape"), plan

    staging = os.path.join(gdir, ".unpack-%d" % os.getpid())
    shutil.rmtree(staging, ignore_errors=True)
    made = []
    try:
        os.makedirs(staging)
        with zipfile.ZipFile(src) as z:
            if plan["kind"] == "pkg":
                # Flattened: a package's own name is its identity, and the folder it sat in inside
                # the archive tells a reader nothing.
                for n in plan["pkgs"]:
                    with z.open(n) as fh, io.open(os.path.join(staging,
                                                               os.path.basename(n)), "wb") as out:
                        shutil.copyfileobj(fh, out, 1 << 20)
                for n in sorted(os.listdir(staging)):
                    _replace_file(os.path.join(gdir, n), os.path.join(staging, n))
                    made.append(n)
            else:
                wanted = []
                app = plan.get("app_root") or ""
                tid = plan.get("tid") or (app.rsplit("/", 1)[-1] if app else "")
                dests = {}
                if app:
                    wanted.append((app, tid or os.path.basename(app)))
                for d in plan.get("data_roots") or []:
                    wanted.append((d, os.path.join(EXTRAS_DIR, d.rsplit("/", 1)[-1])))
                if not wanted:
                    wanted.append(("", EXTRAS_DIR))       # a bare data drop
                for prefix, rel in wanted:
                    base = os.path.join(staging, rel)
                    pre = (prefix + "/") if prefix else ""
                    for n in z.namelist():
                        nn = n.replace("\\", "/")
                        if not nn.startswith(pre):
                            continue
                        tail = nn[len(pre):]
                        if not tail:
                            continue
                        dst = os.path.join(base, *tail.rstrip("/").split("/"))
                        # AN EMPTY FOLDER IN THE ARCHIVE IS STILL AN INSTRUCTION. PS5SX2 ships
                        # PCSX2/bios/ and PCSX2/games/ with nothing in them: that is where the
                        # owner's BIOS dump and discs go, and an emulator that finds no such folder
                        # reports a broken install rather than an empty library.
                        if nn.endswith("/"):
                            if not os.path.isdir(dst):
                                os.makedirs(dst)
                            continue
                        dd = os.path.dirname(dst)
                        if dd and not os.path.isdir(dd):
                            os.makedirs(dd)
                        with z.open(n) as fh, io.open(dst, "wb") as out:
                            shutil.copyfileobj(fh, out, 1 << 20)
                    dests[rel.replace("\\", "/")] = prefix
                for _prefix, rel in wanted:
                    _replace_dir(os.path.join(gdir, rel), os.path.join(staging, rel))
                    made.append(rel.replace("\\", "/"))
                    if tag and app and rel == (tid or os.path.basename(app)):
                        write_release_note(os.path.join(gdir, rel), tag, repo,
                                           os.path.basename(src))
                # WHERE EACH DATA TREE GOES ON THE CONSOLE, in the upstream's own words, carried in
                # the curated entry. No path is invented here: an entry that does not say gets a
                # note with no destination and the panel says the files are on this PC and nothing
                # more. Written AFTER the move, into the folder that survived it: a note written in
                # the staging tree sat in the one directory _replace_dir never moved (it moves the
                # trees inside _extras, not _extras itself), so it was deleted with the staging area
                # and every unpack reported an extras folder with no note in it.
                dmap = {}
                for name, where in ((item.get("data_dirs") or {}).items()):
                    dmap[str(name)] = str(where)
                ex = os.path.join(gdir, EXTRAS_DIR)
                if os.path.isdir(ex):
                    note = {"from": os.path.basename(src), "dirs": {}}
                    for n in sorted(os.listdir(ex)):
                        if n == EXTRAS_NOTE:
                            continue
                        note["dirs"][n] = dmap.get(n, "")
                    with io.open(os.path.join(ex, EXTRAS_NOTE), "w", encoding="utf-8") as f:
                        f.write(json.dumps(note, indent=1, sort_keys=True))
    except Exception as e:
        shutil.rmtree(staging, ignore_errors=True)
        return False, "The archive did not open (%s)." % e.__class__.__name__, plan
    shutil.rmtree(staging, ignore_errors=True)

    # THE ARCHIVE IS KEPT. Renaming it is what stops the scan offering it as an inert .zip item
    # beside the folder it just became - two entries for one thing, one of them unusable.
    kept = src + UNPACKED_SUFFIX
    try:
        if os.path.exists(kept):
            os.remove(kept)
        os.rename(src, kept)
    except OSError:
        pass
    say("[payloads] unpacked %s -> %s" % (os.path.basename(src), ", ".join(made)))
    plan["made"] = made
    what = "the app folder" if plan["kind"] == "app" else (
        "the package" if plan["kind"] == "pkg" else "the files")
    extra = ""
    if len(made) > 1:
        extra = " and %d set%s of files beside it" % (len(made) - 1, "" if len(made) == 2 else "s")
    return True, "Opened %s%s." % (what, extra), plan


def extras_for(cfg, item):
    """The data trees unpacked beside this app, and where each belongs on the console.

    Read from the note unpack_zip() left in _extras/, so the destination is whatever the upstream
    said at the time rather than something inferred here. A tree with no recorded destination is
    returned with an empty one: the panel says the files are on this PC and stops, because putting
    somebody's emulator data in a guessed directory is worse than not putting it anywhere.
    """
    rel = item.get("path") or ""
    if not rel:
        return []
    root = source_root(cfg)
    gdir = os.path.dirname(os.path.join(root, *rel.split("/")))
    ex = os.path.join(gdir, EXTRAS_DIR)
    note = os.path.join(ex, EXTRAS_NOTE)
    if not os.path.isfile(note):
        return []
    try:
        with io.open(note, encoding="utf-8") as f:
            doc = json.load(f)
    except Exception:
        return []
    out = []
    for name, dest in sorted((doc.get("dirs") or {}).items()):
        d = os.path.join(ex, name)
        if os.path.isdir(d):
            nfiles, total = dir_stats(d)
            out.append({"name": name, "local": d, "dest": str(dest or ""),
                        "files": nfiles, "size": total})
    return out


def send_extras(bridge, cfg, item, log=None, dry_run=False):
    """Copy those trees to the console, creating nothing that is already there.

    `dry_run` reports what this WOULD copy and touches nothing. It is not a nicety: /api/install
    grew a dry run because a two-console test suite installed real games on the owner's console,
    and this step runs BEFORE that route is reached - so for one build "ask what it would do"
    answered by writing 56 files into /data/PCSX2. Measured, on the owner's PS5.

    NOTHING IS OVERWRITTEN. What ships in PS5SX2's PCSX2/ folder is a set of DEFAULTS - gs.ini, the
    per-game .ini files, the renderer flags - and after the first run they are the owner's tuned
    settings. An update that copied them back over the top would quietly undo every change they had
    made, so a file that exists on the console is left exactly as it is and only what is missing is
    written. The empty folders in the archive (bios/, games/) are created, because that is where the
    owner's own BIOS dump and discs go and an emulator that finds no such folder says it is broken.
    """
    say = log or (lambda m: None)
    trees = extras_for(cfg, item)
    if not trees:
        return True, "", 0
    if dry_run:
        said = ["%d %s file(s) would go to %s" % (t["files"], t["name"], t["dest"])
                for t in trees if t["dest"]]
        return True, ("; ".join(said) + "." if said else ""), 0
    wrote = skipped = 0
    placed = []
    for t in trees:
        dest = t["dest"].rstrip("/")
        if not dest:
            continue
        for dp, dn, fn in os.walk(t["local"]):
            sub = os.path.relpath(dp, t["local"]).replace("\\", "/")
            rdir = dest if sub in (".", "") else "%s/%s" % (dest, sub)
            # ONE LISTING PER DIRECTORY, not one per file. console_file_size() asks for the parent
            # directory every time it is called, so using it here would have cost a round trip per
            # file - 60 of them for PCSX2, each with its own timeout.
            try:
                rows = bridge.fs_list(rdir, timeout=15) or []
            except Exception:
                rows = []
            have = set(str(e.get("name") or "") for e in rows if not e.get("dir"))
            if not rows:
                try:
                    bridge.fs_mkdir(rdir, timeout=15)
                except Exception:
                    pass
            for d in dn:                     # bios/ and games/ ship empty and must still exist
                try:
                    bridge.fs_mkdir("%s/%s" % (rdir, d), timeout=15)
                except Exception:
                    pass
            for f in sorted(fn):
                if f in have:
                    skipped += 1
                    continue
                lp = os.path.join(dp, f)
                try:
                    size = os.path.getsize(lp)
                    with io.open(lp, "rb") as fh:
                        ok = bridge.fs_write("%s/%s" % (rdir, f), fh, size=size, timeout=300)
                except Exception:
                    ok = False
                if ok:
                    wrote += 1
                else:
                    return False, "%s could not be copied to %s." % (f, rdir), wrote
        placed.append("%s -> %s" % (t["name"], dest))
    if not placed:
        return True, "", 0
    say("[payloads] extras: %s (%d written, %d already there)"
        % ("; ".join(placed), wrote, skipped))
    if not wrote:
        return True, "Its %s files were already on the console." % trees[0]["name"], 0
    return True, "Put %d %s file(s) in %s." % (wrote, trees[0]["name"],
                                               trees[0]["dest"]), wrote


def remember_version(item, tag, repo, asset_name, root=None):
    """Write a proven version into assets/payloads/known-versions.json.

    Best effort and never fatal: this runs inside a request the panel polls, on a PC that may have
    the repository checked out read-only, and a version we could not write down is a cosmetic loss
    - the app simply proves it again next time. Writes through a .part and renames, because a
    half-written JSON here would make the next BUILD blind rather than just this run.
    """
    sha = (item or {}).get("sha256")
    if not (sha and tag):
        return False
    here = root or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(here, "assets", "payloads", "known-versions.json")
    with _kv_lock:
        try:
            with io.open(p, encoding="utf-8") as f:
                doc = json.load(f)
        except Exception:
            doc = {"versions": {}}
        vers = doc.setdefault("versions", {})
        if vers.get(sha, {}).get("version") == tag:
            return True                         # already known - do not rewrite the file
        vers[sha] = {"version": tag, "from": repo, "asset": asset_name}
        try:
            tmp = p + ".part"
            with io.open(tmp, "w", encoding="utf-8", newline="\n") as f:
                f.write(json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
            os.replace(tmp, p)
            return True
        except Exception:
            try:
                os.remove(p + ".part")
            except OSError:
                pass
            return False


def console_state(ip, timeout=6.0):
    """Everything a console knows about payloads and homebrews, in ONE request.

    console_live() and console_payload_sizes() each asked the same route for a different field,
    which on a PS5 - one accept loop - is two round trips for one answer. This is that answer:

        live  {stem}          what is running, by comparable stem
        have  {stem: size}    the payloads it holds in PB_DIR, i.e. what it could start by itself
        kept  {size: path}    every homebrew package it can reach, on /data or a USB stick

    None when the console did not answer. "It told me nothing is running" and "it did not tell me"
    are different, and only the first should grey a tile.
    """
    if not ip:
        return None
    try:
        with urllib.request.urlopen("http://%s:8710/api/payloads" % ip, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    st = j.get("state")
    if not isinstance(st, dict):
        return None
    out = {"live": None, "have": None, "kept": {},
           # Which build is answering. The panel needs this for OUR OWN tile: the catalogue records
           # what the owner's folder held when the ELF was built, so it can never describe the ELF
           # that is running.
           "version": str(j.get("shop_version") or "").strip()}
    if st.get("live") is not None:
        out["live"] = {proc_stem(n) for n in st["live"] if n}
    if st.get("have") is not None:
        out["have"] = {}
        for e in st["have"]:
            try:
                out["have"][proc_stem(e.get("n"))] = int(e.get("s") or 0)
            except Exception:
                continue
    for e in (st.get("kept") or []):
        try:
            if e.get("s"):
                out["kept"][int(e["s"])] = e.get("p") or e.get("n")
        except Exception:
            continue
    return out


def console_live(ip, timeout=6.0):
    """The payloads a console says are running, as comparable stems, or None if it could not say.

    THE CONSOLE IS THE ONLY ONE THAT CAN SEE SOME OF THEM. A PC can probe a TCP port from outside,
    but nanodns listens on UDP and answers no query sent to it from the LAN even while running -
    measured on both consoles - so the only observable fact is that its port is taken, and only
    something running ON the console can try to take it. The PS5 additionally has Payload Manager's
    process list, which catches kstuff and ShadowMountPlus. Asking the console gets all of that for
    one request; probing from here gets none of it.

    None, not an empty set, when the console did not answer: "it told me nothing is running" and
    "it did not tell me" are different, and the second must not turn every tile grey.
    """
    if not ip:
        return None
    try:
        with urllib.request.urlopen("http://%s:8710/api/payloads" % ip, timeout=timeout) as r:
            j = json.loads(r.read().decode("utf-8", "replace"))
    except Exception:
        return None
    live = ((j.get("state") or {}).get("live"))
    if live is None:
        return None
    return {proc_stem(n) for n in live if n}


def console_file_size(bridge, path, timeout=15):
    """Size of one file on a console, or None when it is not there / could not be asked.

    There is no fs_stat on the bridge - only fs_list - so this asks for the parent directory and
    picks the entry out. That matters more than it sounds: seed_homebrew() guarded on a
    `hasattr(bridge, "fs_stat")` that was never true, so it re-copied a 60 MB package every single
    time instead of noticing the console already had it.
    """
    if not path or bridge is None:
        return None
    parent, _, name = path.rpartition("/")
    if not (parent and name):
        return None
    try:
        rows = bridge.fs_list(parent, timeout=timeout)
    except Exception:
        return None
    if rows is None:
        return None
    for e in rows:
        try:
            if not e.get("dir") and str(e.get("name") or "") == name:
                return int(e.get("size") or 0)
        except Exception:
            continue
    return None


# --------------------------------------------------------------------------- #
# the fleet: this PC, the console, and any other companion on the network       #
# --------------------------------------------------------------------------- #
def fleet_summary(cfg, web_dir):
    """What THIS companion can hand over, compact enough to advertise to peers.

    A second PC running this exe has no copy of the owner's folder, so on its own every tile reads
    "not on this PC" and nothing can be pressed. But the bytes exist - on a console, or on the
    machine that does have the folder - and the app is meant to behave as one thing however many
    devices are looking at it. This is the half of that a peer can see.
    """
    cat, _sig = live_catalog(cfg, web_dir)
    out = []
    for it in (cat.get("items") or []):
        # OUR OWN ARTIFACTS ARE ADVERTISED NOW. They were skipped here on the reasoning that every
        # machine builds its own, but that is not the situation the owner is in: the PS5's ELF is
        # 34 MB and deliberately not bundled in the exe, so a second PC has no copy at all and the
        # tile read "not on this PC" for the one payload the fleet most obviously has - while the PC
        # that built it sat on the same LAN. peer_with() matches on size, so a peer can only stand
        # in for a byte-identical build, and ask_peer() has the peer run its OWN deploy lane
        # (version-expect, .prev backup, pldmgr staging), not a raw copy of bytes we shipped it.
        if not local_path(cfg, it):
            continue
        out.append({"id": it.get("id"), "platform": it.get("platform"),
                    "kind": it.get("kind"), "size": it.get("size", 0),
                    "sha256": it.get("sha256", ""), "version": it.get("version", "")})
    return out


def peer_with(peers, item):
    """The first peer advertising this exact item, or None.

    Matched on id + platform + size: the id says which project, and the size says it is the same
    build. A peer holding an older release of the same payload is not a substitute for this one.
    """
    want_id = (item or {}).get("id")
    want_plat = str((item or {}).get("platform") or "").upper()
    want_size = int((item or {}).get("size") or 0)
    # SIZE IS NOT PART OF THE MATCH FOR OUR OWN ARTIFACT, and the difference is the whole question
    # being asked. For a third-party payload it is "is this the same build the tile is describing?",
    # and a peer holding a different ftpsrv is not a substitute - it would quietly downgrade or
    # upgrade something the owner chose. For OUR app the question is "can anyone here give me the
    # shop at all?", and any build of it is an answer: a PC with no folder has no copy to be
    # inconsistent with, and the peer runs its own deploy lane and sends whatever it actually has.
    #
    # Measured, which is why this is not a hypothetical: a second PC's baked catalogue recorded our
    # PS5 ELF at 34,139,632 bytes (the 3.83.3 copy that was in the folder when its exe was built),
    # the PC beside it was advertising the 45,350,920-byte 3.86.0 build, and the sizes disagreeing
    # made the tile read "nobody has this" about a file on the same LAN.
    ours = bool((item or {}).get("ours"))
    for p in (peers or []):
        if not p.get("online"):
            continue
        for e in (p.get("payloads") or []):
            if (e.get("id") == want_id
                    and str(e.get("platform") or "").upper() == want_plat
                    and (ours or not want_size or int(e.get("size") or 0) == want_size)):
                return p
    return None


def ask_peer(peer, action, body, timeout=900):
    """Have another companion carry out an action this one cannot.

    IT IS THE SAME REQUEST, SENT ONE HOP FURTHER. The peer has the file and the same LAN access to
    the same consoles, so the honest way for a PC without the folder to start a payload is to ask a
    PC that has it - not to invent a way of moving bytes it does not hold. The reply is passed back
    untouched, so the owner reads the peer's own words.
    """
    url = str(peer.get("url") or "").rstrip("/")
    if not url:
        return None
    try:
        data = json.dumps(dict(body or {}, via_peer=1)).encode("utf-8")
        req = urllib.request.Request(url + "/api/payloads/" + action, data=data,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as ex:
        # THE PEER ANSWERED. urlopen raises for every 4xx and 5xx, and every refusal the peer can
        # emit - "that one is for a PS5 and this console is a PS4", "is already in your folder",
        # "out of space" - arrives as one of those. Swallowed, it became indistinguishable from a
        # peer that was switched off, and this PC printed its own wrong sentence instead of the
        # one the machine holding the file had just given us.
        try:
            return json.loads(ex.read().decode("utf-8", "replace"))
        except Exception:
            return None
    except Exception:
        return None

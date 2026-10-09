"""One command that answers "is everything actually working?" - PC first, then the console.

Written after a session where the same facts were re-established five times by hand. Everything it
checks was a real bug at some point; each check names the thing it is guarding against.

    python tools/ready_check.py                 # wait for the console, then check everything
    python tools/ready_check.py --offline       # only what can be checked with no PS5
    python tools/ready_check.py --wait 600      # how long to wait for the console (default 180s)
    python tools/ready_check.py --route         # ALSO run the transfer test (copies a real
                                                # package to the drive you pick; off by default)
    python tools/ready_check.py --test-delete   # ALSO prove our file API writes and deletes for
                                                # real, in the shop's own folder - never in a
                                                # ShadowMount watch folder

It never installs a PKG and never deletes one of your games: the delete lane is exercised with
a title id no console can carry (PMSX99999) and must answer "nothing to delete". Nothing it
writes goes into a ShadowMount watch folder (/data/homebrew, /mnt/*/homebrew) under a final name.

The console address comes from --ip, else from the config the exe actually uses (Desktop, then
companion/dist, then companion/config.json - see tools/verify_console.py). It never defaults to
a guessed address, and the console is never handed 127.0.0.1 as a source.
"""
import argparse
import hashlib
import io
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
# One resolver for "which console, and what address is this PC on from there" - the same one
# verify_console.py uses, so the two tools can never measure different machines again.
from verify_console import _route_ip, config_candidates, console_ip_from_config  # noqa: E402

G, Y, R, B, X = "\033[92m", "\033[93m", "\033[91m", "\033[94m", "\033[0m"

# A title id no console can carry: the prefix is not one Sony issues. The old probe used
# CUSA00099 - a plausible retail id - and the delete lane REALLY removes containers, so a
# matching backup would have been deleted by a readiness check. The assertion is that the
# console answers "nothing to delete".
IMPOSSIBLE_TID = "PMSX99999"
# Outside every ShadowMount watch folder and every drive root: the shop's own data folder.
PROBE_DIR = "/data/pkg-mutant-shop/probe"

FAILED, CHECKS, NOTES = [], [0], []


def rec(group, label, ok, detail=""):
    CHECKS[0] += 1
    if not ok:
        FAILED.append("%s: %s  %s" % (group, label, detail))
    print("  [%s%s%s] %-52s %s" % (G if ok else R, "PASS" if ok else "FAIL", X, label, detail))
    return ok


def head(t):
    print("\n%s== %s ==%s" % (B, t, X))


def get(url, timeout=20, body=None):
    """Returns (ok, parsed_or_text, err). A 4xx still carries the server's JSON - read it, the way
    the browser does. urlopen raises on non-2xx; fetch() in the UI does not."""
    try:
        if body is None:
            r = urllib.request.urlopen(url, timeout=timeout)
        else:
            r = urllib.request.urlopen(urllib.request.Request(
                url, data=json.dumps(body).encode(),
                headers={"Content-Type": "application/json"}), timeout=timeout)
        raw = r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        try:
            raw = e.read().decode("utf-8", "replace")
        except Exception:
            return False, None, "HTTP %s" % e.code
        try:
            return True, json.loads(raw), "HTTP %s" % e.code
        except ValueError:
            return False, raw, "HTTP %s" % e.code
    except Exception as e:
        return False, None, e.__class__.__name__
    try:
        return True, json.loads(raw), ""
    except ValueError:
        return True, raw, ""


def port_open(ip, port, t=2.0):
    s = socket.socket()
    s.settimeout(t)
    try:
        return s.connect_ex((ip, port)) == 0
    finally:
        s.close()


# ------------------------------------------------------------------------------------------------
# OFFLINE - the things that do not need a PS5
# ------------------------------------------------------------------------------------------------
def offline():
    head("the build itself")
    ver = {}
    src = io.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()
    m = re.search(r'^VERSION = "([^"]+)"', src, re.M)
    ver["companion"] = m.group(1) if m else "?"
    c = io.open(os.path.join(ROOT, "ps5-app", "onconsole", "server.c"), encoding="utf-8").read()
    m = re.search(r'#define SHOP_VERSION "([^"]+)"', c)
    ver["elf"] = m.group(1) if m else "?"
    h = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    m = re.search(r'var APP_VERSION="([^"]+)"', h)
    ver["ui"] = m.group(1) if m else "?"
    # The two artifacts shipping different numbers is how you lose an afternoon to "which build is
    # actually running". stamp_version.py stamps all three from companion/server.py.
    # "?" means the regex found nothing. Three unknowns are all equal to each other, so without
    # this the check goes green precisely when it has learned nothing.
    rec("build", "all three carry the same version",
        len(set(ver.values())) == 1 and "?" not in ver.values(),
        "companion=%(companion)s elf=%(elf)s ui=%(ui)s" % ver)

    # The ELF embeds the UI. A UI fix that is not re-bundled is invisible on the console.
    # A HASH, not a length: comparing byte counts let a same-size edit ship an ELF whose console
    # UI differed from web/index.html while this stayed green - and it is the one guard for the
    # "console ran the old UI" failure. gen_web_bundle.py now writes a sha256 comment per file;
    # a bundle from before that has none, so its bytes are decoded out of the octal literal.
    bundle = io.open(os.path.join(ROOT, "ps5-app", "onconsole", "web_bundle.h"),
                     encoding="utf-8", errors="replace").read()
    disk_sha = hashlib.sha256(open(os.path.join(ROOT, "web", "index.html"), "rb").read()).hexdigest()
    emb_sha, how = _embedded_sha(bundle, "index.html")
    rec("build", "the embedded web bundle matches web/index.html",
        bool(emb_sha) and emb_sha == disk_sha,
        "%s bundle=%s disk=%s" % (how, (emb_sha or "?")[:12], disk_sha[:12]))

    for tool, args in (("check_web.py", []), ("message_report.py", ["--check"]),
                       # A control byte in source hides itself: a terminal prints 0x08 by
                       # moving the cursor back, so a broken regex renders as if it were
                       # right and the check it belongs to passes whatever the code does.
                       ("check_control_chars.py", []),
                       # The public page and the release notes are part of what ships. The README's
                       # badges went three releases and three firmwares stale before a person
                       # noticed, and check_docs also runs stamp_version - a reworded stamp point
                       # still has every value correct while ABORTING both console builds.
                       ("check_docs.py", []),
                       # A header child with no `order` does not stay where it was on a phone - it
                       # jumps to the FRONT, and nothing else in the suite looks at the layout.
                       ("check_mobile.py", []),
                       # A console must never stop answering because a PC is switched off. One
                       # unbounded connect() on the single accept loop took the whole PS5 shop down
                       # whenever the owner's laptop was off - the page would not even load - and
                       # nothing in this suite could see it, because every gate here talks to a
                       # console that is working.
                       ("check_console_net.py", []),
                       # EVERY LOCAL SUITE, because "python tools/ready_check.py - that is every
                       # gate" is what CONTRIBUTING.md promises and nine of them were not in this
                       # list. They cost about seventeen seconds between them and each pins
                       # something that has gone wrong here before. The only one still left out is
                       # test_fleet_two_consoles.py, which needs BOTH consoles awake and skips
                       # itself otherwise - a gate that silently skips is not one this can rely on.
                       ("lint_python.py", []),
                       ("test_panel_rules.py", []),
                       ("test_etag.py", []),
                       ("test_job_claim.py", []),
                       ("test_viewer_platform.py", []),
                       ("test_reconcile_consoles.py", []),
                       ("test_pkg_serving.py", []),
                       ("test_psn_blocker.py", []),
                       ("i18n_report.py", ["--check"]), ("test_storage_tiles.py", []),
                       ("test_mods_tabs.py", []),
                       ("test_console_tracker.py", []),
                       # Which console the mods panel names on the wire. One line, invisible on a
                       # single-console setup, and it once sent a PS4 game's toggles to the PS5.
                       ("test_mods_console.py", []),
                       # The game panel's facts must describe the CHOSEN console. An update
                       # installed on the PS5 showed as "Installed" with the PS4 selected, so the
                       # owner could not install it onto the PS4 at all.
                       ("test_panel_console_state.py", []),
                       # An INCOMPLETE exe sitting in the repo looks exactly as legitimate as the
                       # real one in a folder listing, and copying it gives a PS4 that never works
                       # with nothing on screen to explain why. That cost a round of debugging.
                       ("check_stale_exe.py", []),
                       # Compiles the in-game agent and the shop's client from the shipped sources
                       # and runs them against each other. It is here and not merely available
                       # because both faults it covers - a protocol mismatch that made every
                       # memory read fail, and a SIGPIPE that killed the game - were invisible
                       # from the panel and cost real time on a console to find.
                       ("test_agent_protocol.py", []),
                       # /data/GoldHEN/plugins.ini is the OWNER'S file - every other plugin they
                       # run is listed in it, and it outlives a reboot and a re-jailbreak. This
                       # lifts the real functions out of the payload and drives them against a
                       # temporary copy; it found six defects at once, one of which failed every
                       # toggle on the console because rename() there will not replace a file.
                       ("test_gh_ini.py", []),
                       # THE CHEAT ENGINE ITSELF, compiled here against a fake game. Both consoles
                       # run one copy of this byte logic (ps4_sync_cheat_core keeps them equal), and
                       # reading it missed a mod applied half-way (which crashed Dark Souls II) and a
                       # master code that was never applied at all (which is why five of that game's
                       # seven cheats could not be turned on). It reads the real shipped cheat file
                       # and sweeps all 1,761 Trainer files through the converter.
                       ("test_cheat_core.py", []),
                       # Porting a cheat between builds is mostly a matter of REFUSING, and every
                       # refusal here protects a game from a plausible-looking wrong address.
                       ("test_cheat_port.py", []),
                       # The signal that makes the Updates section react to an install, an update or
                       # a delete made anywhere. Its failure modes are all "it fired when it should
                       # not have", which would reload the library for ever.
                       ("test_content_signal.py", []),
                       # The cheat library the exe ships to a PS4. A stale or missing archive is
                       # not a build error - PyInstaller fails on a missing data file, loudly -
                       # but a STALE one ships an old library to every console that asks the PC
                       # for cheats, silently, which is the failure worth a gate.
                       ("pack_cheats.py", ["--check"]),
                       # PAYLOADS & HOMEBREWS. The catalogue is generated from the owner's folder
                       # and the payload ELFs are copied out of it into the two onconsole/payloads
                       # directories the .incbin paths point at, so both can silently fall behind
                       # the folder they came from - which is exactly the shape of staleness this
                       # file already exists to catch for the exe and the cheat pack.
                       ("gen_payload_catalog.py", ["--check"]),
                       ("sync_payload_bins.py", ["--check"]),
                       ("test_payloads.py", [])):
        p = subprocess.run([sys.executable, os.path.join(HERE, tool)] + args,
                           capture_output=True, text=True)
        rec("build", tool, p.returncode == 0, (p.stdout or p.stderr).strip().splitlines()[-1][:70]
            if (p.stdout or p.stderr).strip() else "")

    head("guards for regressions that have come back before")
    # Each of these was fixed by hand once and is a one-line edit away from returning.
    # The dock rebuilt every 1.2 s with innerHTML destroyed buttons mid-press. renderQueue() must
    # update rows in place: compare-before-write on the cells, never the list from the tasks.
    rq = _js_function(h, "renderQueue")
    # the empty-state placeholder may be assigned wholesale; a list built from the tasks may not
    rebuilds = [l for l in rq.split("\n")
                if re.search(r'\bbody\.innerHTML\s*\+?=', l) and re.search(r'\.map\(|\.join\(|forEach\(', l)]
    rec("guard", "renderQueue() updates rows in place",
        bool(rq) and ".innerHTML!==" in rq and not rebuilds,
        "compare-before-write present" if ".innerHTML!==" in rq else "no in-place update found")
    # The icon form of the notification call returns 0 and draws NOTHING on 12.70 - it has
    # silenced every console message twice. notify() and notifyf() must pass a NULL icon.
    icon_calls = [a for a in re.findall(r'\bnotify_icon\(([^()]*)\)', c)
                  if a.strip() and not a.startswith("const char")]   # skip the definition and comments
    rec("guard", "console toasts use the plain (NULL icon) form",
        "static void notify(const char *msg) { notify_icon(msg, NULL); }" in c
        and icon_calls and all(re.search(r',\s*NULL\s*$', a) for a in icon_calls),
        "%d notify_icon call(s)" % len(icon_calls))
    # A blanket long max-age once cached index.html, so a new ELF left the console on the old UI
    # for a week. The app shell must revalidate every time.
    #
    # send_file_req, NOT send_file: the body moved when conditional GET was added and send_file is
    # now a one-line wrapper passing NULL for the request. This guard went on reading the wrapper
    # and failed on a payload whose cache policy was perfectly correct - which is the cheapest kind
    # of false alarm to leave lying around, and the kind that gets a whole suite ignored.
    _sf = _c_function(c, "send_file_req")
    rec("guard", "send_file_req() serves the app shell no-cache",
        '"no-cache, must-revalidate"' in _sf)
    # And the 304 has to carry the SAME policy. A revalidation that answered without Cache-Control
    # would let a browser fall back to its own heuristics and keep a shell it had just been told to
    # re-check - the identical bug, arriving by the new route.
    rec("guard", "the 304 repeats the cache policy",
        "304 Not Modified" in _sf and _sf.count("Cache-Control") >= 2,
        "%d Cache-Control header(s) in send_file_req" % _sf.count("Cache-Control"))
    # An add-on is a PKG, never a container - inheriting the game's ffpfsc format once routed a
    # 1 MB DLC down the mount lane and reported a finished install as a bad dump.
    try:
        sys.path.insert(0, os.path.join(ROOT, "companion"))
        import server as _srv
        rec("guard", "is_backup_item(): a DLC under a ffpfsc game is not a backup",
            _srv.is_backup_item({"kind": "dlc"}, {"format": "ffpfsc"}) is False
            and _srv.is_backup_item({"kind": "backup"}) is True
            and _srv.is_backup_item({"file": "x.ffpfsc"}) is True)
    except Exception as e:
        rec("guard", "is_backup_item(): a DLC under a ffpfsc game is not a backup", False, repr(e)[:60])

    # The page's own assertions. See ui_checks(): these were unreachable.
    ui_checks(h)



def ui_checks(h):
    """Every assertion about the SHIPPED page, given its text.

    THIS WHOLE BLOCK WAS UNREACHABLE. It sat after the `return` of _embedded_sha(), so Python
    parsed it, never ran it, and the fourteen names it reads out of `h` were simply undefined.
    None of these checks had fired since the day they were pasted here - including the one
    guarding the rule that `lane` may only be read inside isBackupTitle(), which is the exact
    regression this file was extended to catch.

    Same failure this tool has had before and in the same shape: a gate that reports nothing
    is indistinguishable from a gate that reports success. tools/lint_python.py now fails a
    build on an undefined name, which is what found it.
    """
    head("the UI's own logic, run against the shipped file")
    # These functions decide whether a game can be sent to a drive of your choosing. Gating them on
    # `lane` was what silently removed the destination picker: no live title carries lane "mount".
    for fn in ("isBackupTitle", "backupDriveOf", "ps5Destinations", "driveLabelOf"):
        rec("ui", "%s() is present" % fn, ("\nfunction %s(" % fn) in h)
    # `lane` is not a reliable backup test - no live title carries "mount" - so isBackupTitle() is
    # the ONLY place allowed to read it. Comments are stripped first, otherwise the note explaining
    # this very rule counts as a violation of it.
    code = re.sub(r"/\*.*?\*/", "", h, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    m = re.search(r"\nfunction isBackupTitle\(", code)
    inside = ""
    if m:
        i = code.index("{", m.end())
        depth, j = 0, i
        while j < len(code):
            if code[j] == "{":
                depth += 1
            elif code[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        inside = code[i:j + 1]
    # A GAME's lane, not a TASK's. The rule is about library entries: `lane` is absent on every
    # live title (measured: 0 of 112), so gating a game's behaviour on it silently removes the
    # control it guards. A QUEUE TASK is a different object with a different producer - the install
    # route sets "lane":"mount" on the row it creates - so tk.lane is both reliable and the right
    # thing to read, and renderQueue does exactly that.
    #
    # The first version of this counted every `lane==="mount"` in the file and so flagged the queue
    # row as a violation. It counts receivers now: anything whose object is not a task.
    hits = re.findall(r'(\w+)\.lane==="mount"', code)
    total = [r for r in hits if r not in ("tk", "t", "task")]
    inside_hits = re.findall(r'(\w+)\.lane==="mount"', inside)
    allowed = [r for r in inside_hits if r not in ("tk", "t", "task")]
    rec("ui", "only isBackupTitle() reads a GAME's lane===\"mount\"",
        len(total) > 0 and len(total) == len(allowed),
        "%d on a game (%s), %d of them inside isBackupTitle"
        % (len(total), ",".join(sorted(set(total))) or "-", len(allowed)))
    # Settings is ONE PAGE of cards now. The tab machinery is gone, and it must stay gone: the
    # answer to "why will this not install" used to be behind whichever tab you were not on.
    # Column flow, not a grid: a grid stretched every row to its tallest card and left a band of
    # dead space before the next row. break-inside is what stops a column splitting a card.
    rec("ui", "settings cards flow in columns",
        ".setgrid{column-count:3" in h and "break-inside:avoid" in h)
    rec("ui", "settings has all eight cards", h.count('<section class="scard') == 8,
        "%d found" % h.count('<section class="scard'))
    rec("ui", "the settings tab machinery is gone",
        "showSetTab" not in h and "settab" not in h and "setpane" not in h)
    # applyI18n() assigns textContent, so a title carrying data-i18n directly would wipe its icon.
    # The key has to sit on an inner span - and every card must still carry one, or the seven
    # translations stop reaching the headings.
    rec("ui", "every card title is translatable", h.count('</svg></span><span data-i18n="sec_') == 8,
        "%d of 8" % h.count('</svg></span><span data-i18n="sec_'))
    # The five saved settings must remain real inputs/selects with these exact ids: openSettings()
    # dereferences setIp and setPort UNGUARDED, so losing one stops the panel opening at all.
    for _id in ("setIp", "setPort", "setParallel", "setVerify", "setAutoScan"):
        rec("ui", "saved control %s survives" % _id, ('id="%s"' % _id) in h)
    rec("ui", "no title= tooltips left (the PS5 browser never shows them)",
        'title="' not in h, "%d found" % h.count('title="'))


def _js_function(h, name):
    """The body of `function name(` in the page, comments stripped. "" when absent."""
    code = re.sub(r"/\*.*?\*/", "", h, flags=re.S)
    code = re.sub(r"^\s*//.*$", "", code, flags=re.M)
    m = re.search(r"\nfunction %s\(" % re.escape(name), code)
    if not m:
        return ""
    i = code.index("{", m.end())
    depth, j = 0, i
    while j < len(code):
        if code[j] == "{":
            depth += 1
        elif code[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return code[i:j + 1]


def _c_function(c, name):
    """The body of a C function defined at column 0. "" when absent."""
    m = re.search(r"^static\s+[\w \*]+?\b%s\s*\(" % re.escape(name), c, re.M)
    if not m:
        return ""
    i = c.index("{", m.end())
    depth, j = 0, i
    while j < len(c):
        if c[j] == "{":
            depth += 1
        elif c[j] == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    return c[i:j + 1]


def _embedded_sha(bundle, rel):
    """(sha256, how) of one embedded file in web_bundle.h, or (None, why)."""
    m = re.search(r'/\* %s\s+\d+ bytes\s+sha256 ([0-9a-f]{64}) \*/' % re.escape(rel), bundle)
    if m:
        return m.group(1), "stamped"
    m = re.search(r'\{"%s", (\w+), (\d+)\}' % re.escape(rel), bundle)
    if not m:
        return None, "not in bundle"
    name = m.group(1)
    i = bundle.find("static const unsigned char %s[] =" % name)
    j = bundle.find("\n;", i) if i >= 0 else -1
    if i < 0 or j < 0:
        return None, "literal not found"
    data = bytes(int(o, 8) for o in re.findall(r'\\([0-7]{3})', bundle[i:j]))
    if len(data) != int(m.group(2)):
        return None, "decoded %d of %s bytes" % (len(data), m.group(2))
    return hashlib.sha256(data).hexdigest(), "decoded"

# ------------------------------------------------------------------------------------------------
# ONLINE
# ------------------------------------------------------------------------------------------------
def js_predicate_check(pc_lib, con_lib, devices):
    """Run the REAL functions out of index.html in node, against the REAL library."""
    h = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()

    def grab(name):
        m = re.search(r"\nfunction %s\(" % re.escape(name), h)
        if not m:
            return ""
        i = h.index("{", m.end())
        depth, j = 0, i
        while j < len(h):
            if h[j] == "{":
                depth += 1
            elif h[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        return h[m.start() + 1:j + 1]

    def grab_var(name):
        m = re.search(r"\nvar %s=" % re.escape(name), h)
        return h[m.start() + 1:h.index(";", m.end()) + 1] if m else ""

    js = "\n".join([
        grab_var("MOUNT_FMTS"), grab_var("PS5_DESTS"),
        grab("isBackupTitle"), grab("backupDriveOf"), grab("ps5Destinations"),
        "var state={devices:%s};" % json.dumps({"ps5": devices}),
        "var PC=%s, CON=%s;" % (json.dumps(pc_lib), json.dumps(con_lib)),
        """
var out={};
function scan(g){var b=0,miss=0,wrong=0;
  g.forEach(function(x){
    var is=isBackupTitle(x);
    if(is) b++;
    if(x.backup_path && !is) miss++;
    if(is && !x.backup_path && x.source!=="backup" && x.lane!=="mount" &&
       !(x.format&&MOUNT_FMTS[String(x.format).toLowerCase()]) &&
       !(x.base&&x.base[0]&&x.base[0].kind==="backup")) wrong++;
  });
  return {n:g.length,b:b,miss:miss,wrong:wrong};}
out.pc=scan(PC.games||[]); out.con=scan(CON.games||[]);
var all=ps5Destinations();
out.dest={known:all.length, seen:all.filter(function(d){return d.detected!==false;}).length,
          list:all.filter(function(d){return d.detected!==false;}).map(function(d){return d.id;})};
console.log(JSON.stringify(out));
""",
    ])
    f = os.path.join(ROOT, ".ready_check.js")
    io.open(f, "w", encoding="utf-8").write(js)
    try:
        p = subprocess.run(["node", f], capture_output=True, text=True, timeout=60)
        return json.loads(p.stdout.strip().splitlines()[-1]) if p.returncode == 0 else None
    except Exception:
        return None
    finally:
        try:
            os.remove(f)
        except OSError:
            pass


def online(a):
    con = "http://%s:%d" % (a.ip, a.shop_port)
    pc = a.companion

    head("reachability")
    rec("net", "the PC companion answers", port_open("127.0.0.1", 8710), pc)
    ok, ph, _ = get(pc + "/api/health", 15)
    rec("net", "companion health", ok and isinstance(ph, dict) and ph.get("ok"),
        "v%s" % (ph or {}).get("version"))
    rec("net", "the console shop answers", port_open(a.ip, a.shop_port), con)
    ok, ch, _ = get(con + "/api/health", 15)
    rec("net", "console health", ok and isinstance(ch, dict) and ch.get("ok"),
        "v%s built %s" % ((ch or {}).get("version"), (ch or {}).get("built")))
    # None == None is true, so with neither side answering this passed while proving nothing.
    rec("net", "both sides are the same version",
        bool((ph or {}).get("version")) and (ph or {}).get("version") == (ch or {}).get("version"),
        "%s vs %s" % ((ph or {}).get("version"), (ch or {}).get("version")))
    # AND THE THIRD SIDE. This called two "both sides" while stamp_version.py stamps the PS4's own
    # SHOP_VERSION and /api/health reports it - so the one artifact nobody checked was the one this
    # project has twice caught running a build behind. Skipped, not failed, when no PS4 answers:
    # a console that is switched off is not a version mismatch.
    _, pdev, _ = get(pc + "/api/devices", 15)
    p4 = ""
    for c in ((pdev or {}).get("consoles") or []):
        if str(c.get("platform") or "").lower() == "ps4" and c.get("ip"):
            p4 = "http://%s:%d" % (c["ip"], int(c.get("shop_port") or 8710))
            break
    if p4:
        ok4, h4, _ = get(p4 + "/api/health", 10)
        _v4 = (h4 or {}).get("version")
        if not _v4:
            # A CONSOLE THAT DOES NOT ANSWER IS NOT A VERSION MISMATCH - which is what the comment
            # above already says, but the skip only covered a PS4 that was not CONFIGURED. A
            # configured one that is switched off, asleep, or holding its port open without serving
            # (an earlier instance whose accept loop has died does exactly that) was reported as
            # "None vs 3.97.0", a sentence about a version nobody read. Named for what it is, so a
            # release is not blocked by a console being off while a real mismatch still is.
            rec("net", "the PS4 payload is the same version too", True,
                "no answer from the PS4 - skipped (off, asleep, or its shop is not serving)")
        else:
            rec("net", "the PS4 payload is the same version too",
                _v4 == (ph or {}).get("version"),
                "%s vs %s" % (_v4, (ph or {}).get("version")))
    else:
        rec("net", "the PS4 payload is the same version too", True, "no PS4 answering - skipped")
    # Third-party install hosts must be absent - the whole point of our own engine.
    #
    # 9090 IS NOT ONE OF THEM AND MUST NEVER BE OPENED. That is GoldHEN's BinLoader: the jailbreak's
    # own payload loader, the only way a new build of our PS4 ELF gets loaded, and a port that stops
    # listening when something connects to it and closes again without POSTing an ELF. This check
    # used to include it, so every gate run killed the loader - and the next push failed with
    # "the loader did not take it", on a console whose config.ini still said Enabled = 1. The one
    # cure is the owner toggling it in GoldHEN's menu, which is a high price for a line of a report.
    # companion/payloads.py's module header has said "never TCP-probe a PS4's :9090" all along.
    rec("net", "no third-party install host is listening",
        not any(port_open(a.ip, p, 1.5) for p in (12800, 9081, 9040)),
        "12800/9081/9040 closed (9090 is GoldHEN's loader and is never probed)")
    dead = [k for k in (ch or {}) if k.startswith("dpi")] + \
           [k for k in (ch or {}) if k == "capabilities"]
    rec("net", "health carries no purged etaHEN/DPI fields", not dead, ", ".join(dead) or "clean")

    head("the library, and what it says about each game")
    ok, pl, _ = get(pc + "/api/library", 60)
    ok2, cl, _ = get(con + "/api/library", 60)
    rec("lib", "both servers return a library",
        ok and ok2 and isinstance(pl, dict) and isinstance(cl, dict),
        "PC=%d CON=%d titles" % (len((pl or {}).get("games", [])), len((cl or {}).get("games", []))))
    backs = [g for g in (pl or {}).get("games", []) if g.get("backup_path")]
    rec("lib", "backups carry movable + local_path (else Move is hidden)",
        bool(backs) and all(g.get("movable") and g.get("local_path") for g in backs),
        "%d of %d" % (sum(1 for g in backs if g.get("movable") and g.get("local_path")), len(backs)))

    ok, dv, _ = get(con + "/api/devices", 20)
    devices = (dv or {}).get("ps5", [])
    res = js_predicate_check(pl or {}, cl or {}, devices)
    if res is None:
        rec("lib", "UI predicate replayed over the live library", False, "node did not run")
    else:
        rec("lib", "every backup is recognised as one (PC)",
            res["pc"]["miss"] == 0, "%d backups, %d missed" % (res["pc"]["b"], res["pc"]["miss"]))
        rec("lib", "every backup is recognised as one (console)",
            res["con"]["miss"] == 0, "%d backups, %d missed" % (res["con"]["b"], res["con"]["miss"]))
        rec("lib", "no PS4 package is mistaken for a backup",
            res["pc"]["wrong"] == 0 and res["con"]["wrong"] == 0)
        rec("lib", "the drive picker offers only connected storage",
            0 < res["dest"]["seen"] < res["dest"]["known"],
            "%d of %d offered: %s" % (res["dest"]["seen"], res["dest"]["known"],
                                      ", ".join(res["dest"]["list"])))

    head("controls that must work from BOTH servers")
    for name, base in (("console", con), ("PC", pc)):
        # IMPOSSIBLE_TID, never a real-looking id: this route deletes for real.
        ok, r, _ = get(base + "/api/game/delete", 30, {"title_id": IMPOSSIBLE_TID})
        rec("both", "%s: delete answers 'nothing to delete' for %s" % (name, IMPOSSIBLE_TID),
            ok and isinstance(r, dict) and r.get("already_gone") is True
            and r.get("deleted") is not True)
        ok, r, _ = get(base + "/api/game/delete", 30, {})
        rec("both", "%s: delete refuses with a reason when no id is sent" % name,
            ok and isinstance(r, dict) and r.get("ok") is False and "title id" in str(r.get("error", "")))
        ok, r, _ = get(base + "/api/move/status", 20)
        rec("both", "%s: move status is served" % name,
            ok and isinstance(r, dict) and "active" in r)
    ok, r, _ = get(con + "/api/queue/console-local/dismiss", 20, {})
    rec("both", "console: the queue X is implemented",
        ok and isinstance(r, dict) and r.get("ok") is True)
    ok, r, _ = get(con + "/api/queue/console-local/retry", 20, {})
    rec("both", "console: the queue Retry is implemented",
        ok and isinstance(r, dict) and "not supported on console" not in json.dumps(r))

    head("the file API writes and deletes for real (never in a watch folder)")
    # THIS USED TO WRITE A 4 KB .ffpfsc INTO /mnt/ext1/homebrew. ShadowMount mounts whatever
    # appears in a watch folder the instant it appears, and a zero-filled file named like a
    # compressed PFS container is precisely the half-written container that has crashed this
    # console before; the .part-then-rename only narrowed the window, because once renamed it was
    # a complete-but-garbage container until the delete landed. So the probe now lives in the
    # shop's own folder, which no scanner reads, and goes through OUR file API - the same
    # /api/fs/write and /api/fs/delete every deploy and cleanup in this project relies on. What
    # this proves: a write lands with the right byte count, stat sees it, delete removes it. The
    # delete-by-title lane is covered above with an id no console can carry.
    if not a.test_delete:
        rec("delete", "file API round trip (skipped - pass --test-delete)", True,
            "writes 4 KB under %s and removes it" % PROBE_DIR)
        return _after_delete(a, con, pc, ph)
    probe = PROBE_DIR + "/ready-check.bin"
    try:
        url = con + "/api/fs/write?path=" + urllib.parse.quote(probe, safe="")
        req = urllib.request.Request(url, data=b"\0" * 4096, method="POST",
                                     headers={"Content-Type": "application/octet-stream",
                                              "Content-Length": "4096"})
        with urllib.request.urlopen(req, timeout=30) as r:
            wr = json.loads(r.read().decode("utf-8", "replace"))
        rec("delete", "our file API accepted a 4 KB write in %s" % PROBE_DIR,
            isinstance(wr, dict) and wr.get("ok") is True, json.dumps(wr)[:60])
        ok, st, _ = get(con + "/api/fs/stat?path=" + urllib.parse.quote(probe, safe=""), 20)
        rec("delete", "stat sees exactly what was written",
            ok and isinstance(st, dict) and st.get("size") == 4096, "size=%s" % (st or {}).get("size"))
        ok, d, _ = get(con + "/api/fs/delete?path=" + urllib.parse.quote(probe, safe=""), 30)
        rec("delete", "delete answers ok", ok and isinstance(d, dict) and d.get("ok") is True)
        ok2, gone, _ = get(con + "/api/fs/stat?path=" + urllib.parse.quote(probe, safe=""), 20)
        still = isinstance(gone, dict) and gone.get("ok")
        if still:
            # once more before calling it a failure - the first stat can race the unlink
            time.sleep(1)
            get(con + "/api/fs/delete?path=" + urllib.parse.quote(probe, safe=""), 30)
            ok2, gone, _ = get(con + "/api/fs/stat?path=" + urllib.parse.quote(probe, safe=""), 20)
            still = isinstance(gone, dict) and gone.get("ok")
        rec("delete", "the probe is really gone", not still)
    except Exception as e:
        rec("delete", "file API round trip", False, repr(e)[:60])

    return _after_delete(a, con, pc, ph)


def _after_delete(a, con, pc, ph):
    """Everything that follows the delete test. Split out so the skip path above can jump here
    without duplicating it."""
    head("the install lane")
    ok, ss, _ = get(con + "/api/engine/spawn-status", 15)
    rec("install", "no install is stuck in flight",
        ok and isinstance(ss, dict) and not ss.get("busy"),
        "busy_for=%ss" % (ss or {}).get("busy_for"))
    ok, fl, _ = get(con + "/api/fs/list?path=" + urllib.parse.quote("/data/pkg-mutant-shop", safe=""), 20)
    names = [e.get("name") for e in (fl or {}).get("entries", [])]
    # The auto-clean removes both handover files after every install. Either one still sitting
    # here means the last install did not finish cleanly.
    rec("install", "the handover files are cleaned up",
        "installer-req.txt" not in names and "installer-res.json" not in names,
        ", ".join(n for n in names if n.startswith("installer")) or "clean")
    ok, fl, _ = get(con + "/api/fs/list?path=" + urllib.parse.quote("/data/pkg-mutant-shop/payloads", safe=""), 20)
    inst = next((e for e in (fl or {}).get("entries", []) if e.get("name") == "pms-installer.elf"), None)
    rec("install", "our installer is on the console", bool(inst and inst.get("size")),
        "%s bytes" % (inst or {}).get("size"))

    if a.route:
        head("does a chosen drive really receive the file?")
        # The fetch lane, which is what puts a PS5 backup on the drive you picked. OPT-IN: it
        # copies a real package over the LAN into a homebrew folder on every run, and a 227 MB
        # probe was once left behind there. The SMALLEST .pkg in the library is used, under a
        # throwaway .bin name ShadowMount ignores, so no game is touched.
        ok, lib, _ = get(pc + "/api/library", 60)
        key, best = None, None
        for g in (lib or {}).get("games", []):
            b = (g.get("base") or [None])[0] or {}
            sz = b.get("size") or 0
            if b.get("install_key", "").lower().endswith(".pkg") and 0 < sz < 300e6 \
                    and (best is None or sz < best):
                key, best = b["install_key"], sz
        if not key:
            rec("route", "a small package to test with", False, "none found in the library")
        else:
            # The URL is fetched BY THE CONSOLE. Loopback is the console itself, and 0x80B22404
            # from there reads as a broken fetch lane when it is only a wrong address. Refuse it,
            # the way verify_console does, rather than "fall back" to it.
            lan = (ph or {}).get("lan_ip") or _route_ip(a.ip)
            if lan in ("127.0.0.1", "localhost", "::1", "0.0.0.0", ""):
                rec("route", "a source address the console can reach", False,
                    "only loopback is known for this PC - the console cannot fetch from that")
                return
            cport = urllib.parse.urlsplit(pc).port or 8710
            src = "http://%s:%d/library/%s" % (lan, cport, key)
            dest = "/mnt/%s/homebrew" % a.route_drive if a.route_drive != "internal" else "/data/homebrew"
            q = ("/api/engine/fetch?url=" + urllib.parse.quote(src, safe="") +
                 "&dest=" + urllib.parse.quote(dest, safe="") +
                 "&name=" + urllib.parse.quote("ZZ-ready-check.bin", safe=""))
            ok, r, _ = get(con + q, 120)
            started = ok and isinstance(r, dict) and r.get("ok")
            rec("route", "the console accepted a transfer to %s" % a.route_drive, started,
                (r or {}).get("path", "")[:52])
            if started:
                landed = False
                for _ in range(40):
                    time.sleep(3)
                    ok, j, _ = get(con + "/api/engine/job", 15)
                    if ok and isinstance(j, dict) and j.get("state") == "done":
                        landed = True
                        break
                    if ok and isinstance(j, dict) and j.get("error"):
                        break
                rec("route", "it finished", landed)
                # OUR OWN file API, not FTP. ftpsrv is a separate payload the user may simply not
                # have loaded - it was not, on this run - and then a passing transfer was reported
                # as a failure and its 227 MB probe was left behind because the cleanup needed FTP
                # too. The shop is by definition running, or none of this test could have got here.
                probe = dest + "/ZZ-ready-check.bin"
                ok, st, _ = get(con + "/api/fs/stat?path=" + urllib.parse.quote(probe, safe=""), 30)
                sz = (st or {}).get("size") if isinstance(st, dict) else None
                rec("route", "the file is on %s, not somewhere else" % a.route_drive,
                    bool(sz), "%s bytes in %s" % (sz, dest))
                get(con + "/api/fs/delete?path=" + urllib.parse.quote(probe, safe=""), 30)
                ok2, gone, _ = get(con + "/api/fs/stat?path=" + urllib.parse.quote(probe, safe=""), 20)
                if isinstance(gone, dict) and gone.get("ok"):
                    # once more: hundreds of MB left in a homebrew folder is worth a second try
                    time.sleep(2)
                    get(con + "/api/fs/delete?path=" + urllib.parse.quote(probe, safe=""), 30)
                    ok2, gone, _ = get(con + "/api/fs/stat?path=" + urllib.parse.quote(probe, safe=""), 20)
                rec("route", "the probe cleaned itself up",
                    not (isinstance(gone, dict) and gone.get("ok")),
                    "" if not (isinstance(gone, dict) and gone.get("ok"))
                    else "STILL THERE: remove %s by hand" % probe)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default=None, help="PS5 address (default: from config.json)")
    ap.add_argument("--shop-port", type=int, default=8710)
    ap.add_argument("--ftp-port", type=int, default=2121)
    ap.add_argument("--companion", default="http://127.0.0.1:8710")
    ap.add_argument("--wait", type=int, default=180, help="seconds to wait for the console")
    ap.add_argument("--offline", action="store_true")
    ap.add_argument("--route", action="store_true",
                    help="run the transfer test (copies the smallest library .pkg to "
                         "--route-drive under a throwaway name, then removes it). OFF by default.")
    ap.add_argument("--no-route", action="store_true", help=argparse.SUPPRESS)  # the old default; kept so old notes still parse
    ap.add_argument("--route-drive", default="usb0")
    ap.add_argument("--test-delete", action="store_true",
                    help="prove our file API writes and deletes for real: a 4 KB probe under "
                         "%s (the shop's own folder - never a ShadowMount watch folder)." % PROBE_DIR)
    a = ap.parse_args()

    if not a.ip and not a.offline:
        a.ip, src = console_ip_from_config()
        if a.ip:
            print("console address %s from %s" % (a.ip, src))
        else:
            # Never a guessed address. 10.0.0.99 used to be the silent default, so a PC with no
            # config would measure whatever answered there.
            raise SystemExit("no --ip, and no console address in any of:\n  "
                             + "\n  ".join(config_candidates()) + "\n(pass --offline for the PC-only checks)")

    print("%sPKG MUTANT SHOP - ready check%s   console %s   companion %s"
          % (B, X, a.ip or "(offline)", a.companion))
    offline()

    if a.offline:
        pass
    else:
        if not port_open(a.ip, a.shop_port, 2.0):
            print("\n%swaiting up to %ds for the shop on %s:%d ...%s"
                  % (Y, a.wait, a.ip, a.shop_port, X))
            t0 = time.time()
            while time.time() - t0 < a.wait and not port_open(a.ip, a.shop_port, 2.0):
                time.sleep(5)
        if port_open(a.ip, a.shop_port, 2.0):
            online(a)
        else:
            print("\n%sThe console never answered. Load PKG-MUTANT-SHOP.elf from Payload Manager,"
                  "\nthen run this again. Everything above is the PC-side result.%s" % (Y, X))

    print("\n%s== result ==%s" % (B, X))
    if FAILED:
        print("  %d checks, %s%d failed%s" % (CHECKS[0], R, len(FAILED), X))
        for f in FAILED:
            print("   %s-%s %s" % (R, X, f))
    else:
        print("  %d checks, %sall passed%s" % (CHECKS[0], G, X))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())

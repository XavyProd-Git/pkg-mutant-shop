# -*- coding: utf-8 -*-
"""The Payloads & Homebrews catalogue and its rules, on a PC, with no console needed.

Every check here was written because getting it wrong has a specific, nameable consequence - and
each one was perturbed and watched to fail before it was believed.
"""
import io
import json
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "companion"))
import payloads as P                                             # noqa: E402

CAT = os.path.join(ROOT, "web", "assets", "payloads-catalog.json")
CURATED = os.path.join(ROOT, "assets", "payloads", "curated.json")

fails = []
n = 0


def ok(cond, what, detail=""):
    global n
    n += 1
    if not cond:
        fails.append("%s%s" % (what, (" - " + detail) if detail else ""))


def main():
    cat = json.load(io.open(CAT, encoding="utf-8"))
    items = cat["items"]
    ok(len(items) >= 10, "the catalogue has entries", "%d" % len(items))

    # ---- identity ------------------------------------------------------------------------------
    for it in items:
        ok(it.get("id"), "every item has an id", str(it)[:80])
        ok(it.get("platform") in ("PS4", "PS5"), "every item names a platform", it.get("id"))
        ok(it.get("kind") in ("payload", "homebrew"), "every item names a kind", it.get("id"))
        ok(it.get("shape") in ("elf", "pkg", "folder"), "every item names a shape", it.get("id"))

    # ---- THE PLATFORM COMES FROM THE FOLDER, NOT FROM A FILENAME -------------------------------
    # A PS5 package handed to a PS4 comes back refused once per press, with the PS4 blamed for it.
    ps4 = [i["id"] for i in items if i["platform"] == "PS4"]
    ps5 = [i["id"] for i in items if i["platform"] == "PS5"]
    # THE FOLDER IS THE RULE, AND THE OWNER MOVES THINGS. FPKGi was PS5-only until the owner split
    # their "PKGI PS4-PS5" folder into one per console, at which point it correctly became
    # available on both - so pinning THAT item as single-platform was pinning a decision that is
    # theirs to change. These two are single-platform by what they are: PS4-Xplorer is a PS4
    # package and the Internet Browser is a PS5 one.
    ok("LAPY20009" in ps4, "the PS4-only homebrew is on the PS4 side")
    ok("LAPY20009" not in ps5, "...and is not offered to the PS5")
    ok("MOUU12023" in ps5, "the PS5-only homebrew is on the PS5 side")
    ok("MOUU12023" not in ps4, "...and is not offered to the PS4")

    # ---- THE SAME TITLE ON BOTH CONSOLES IS TWO ITEMS, NOT A DUPLICATE -------------------------
    item = [i for i in items if i["id"] == "ITEM00001"]
    ok(len(item) == 2, "Itemzflow appears once per console", "%d" % len(item))
    ok({i["platform"] for i in item} == {"PS4", "PS5"}, "...one PS4, one PS5")

    # ---- OUR OWN ARTIFACTS ARE MARKED AND NEVER EMBEDDED ---------------------------------------
    ours = [i for i in items if i.get("ours")]
    ok(len(ours) == 2, "both copies of our own ELF are flagged `ours`", "%d" % len(ours))
    for i in ours:
        ok(i["kind"] == "payload", "...and they are payloads")

    # ---- A JAILBREAK-LAYER PAYLOAD NEVER AUTO-STARTS -------------------------------------------
    # payload_bundle.h states the rule: that is the owner's call, never a side effect of ours.
    for i in items:
        if i.get("layer") == "jailbreak":
            ok(not i.get("autostart"), "a jailbreak-layer payload does not auto-start", i["id"])
    jb = {i["id"] for i in items if i.get("layer") == "jailbreak"}
    ok({"kstuff", "onionhen", "webkit-autoloader-installer"} <= jb,
       "the three jailbreak-layer payloads are marked", ", ".join(sorted(jb)))

    # ---- A PORT OF 0 MEANS "NOTHING TO OBSERVE", NEVER "AUTO-START IT" --------------------------
    # The 9021-vs-10101 mix-up made the "is it up?" test permanently true for months.
    for i in items:
        if i.get("autostart"):
            ok(int(i.get("port") or 0) > 0,
               "anything that auto-starts has a port to test first", i["id"])

    # ---- THE FOLDER-SHAPED APP IS NOT A PACKAGE ------------------------------------------------
    ra = [i for i in items if i["id"] == "PPSA99169"]
    ok(len(ra) == 1 and ra[0]["shape"] == "folder",
       "RetroArch is carried as a folder, not a package")
    ok(ra and ra[0].get("title_id") == "PPSA99169", "...and keeps its real title id")

    # ---- THE UNREADABLE PACKAGE IS CARRIED, FLAGGED, AND STILL NAMED ---------------------------
    # The owner's call: list it and let the console decide. Its name comes from the param.json
    # inside its \x7fFIH container, not from the filename.
    br = [i for i in items if i["id"] == "MOUU12023"]
    ok(len(br) == 1, "the browser package is in the catalogue")
    if br:
        ok(br[0].get("unreadable") is True, "...flagged as a format we do not parse")
        ok(br[0].get("title") == "Internet Browser", "...but still correctly named",
           br[0].get("title"))

    # ---- SERVE KEYS ARE NOT LIBRARY KEYS -------------------------------------------------------
    # Registering one of these as a game would put five homebrews on the owner's shelf, and
    # normalise_pkg_names() would rename their files on disk.
    for i in items:
        if i["kind"] == "homebrew":
            k = P.serve_key(i)
            ok(k.startswith("PMS-HOMEBREW/"), "a homebrew serve key is namespaced", k)

    # ---- STEM MATCHING, THE THING THAT MADE TWO LIVE PAYLOADS LOOK STOPPED ----------------------
    # Payload Manager reports the name a payload was BUILT as, not the filename we ship.
    ok(P.proc_stem("ftpsrv-ps5.elf") == P.proc_stem("ftpsrv.elf"),
       "ftpsrv-ps5.elf matches the running ftpsrv.elf")
    ok(P.proc_stem("pldmgr_v0.5.2.elf") == P.proc_stem("pldmgr.elf"),
       "pldmgr_v0.5.2.elf matches the running pldmgr.elf")
    ok(P.proc_stem("nanodns-ps4.elf") == "nanodns", "the PS4 build matches too")
    ok(P.proc_stem("shadowmountplus.elf") == "shadowmountplus", "an exact name is left alone")
    ok(P.proc_stem("kstuff.elf") != P.proc_stem("ftpsrv.elf"),
       "two different payloads do not collide")

    # ---- A NEWER VERSION IS ONLY EVER A COMPARISON WE COULD ACTUALLY MAKE -----------------------
    ok(P.newer_than("1.7", "1.6") is True, "1.7 is newer than 1.6")
    ok(P.newer_than("1.6", "1.7") is False, "1.6 is not newer than 1.7")
    ok(P.newer_than("", "1.6") is False, "an unknown upstream is never 'newer'")
    ok(P.newer_than("v2.0", "") is False, "an unknown local version is never 'older'")

    # ---- IDENTITY COMES FROM THE FILE, NOT ITS NAME --------------------------------------------
    # The owner renames things and said so. A payload called anything at all must keep its port,
    # its upstream and its jailbreak warning, and a version we can read from inside beats one
    # guessed from a filename.
    byc = [i for i in items if i["kind"] == "payload" and i.get("identified_by") == "content"]
    ok(len(byc) >= 7, "payloads are identified by what is inside them", "%d of them" % len(byc))
    for i in items:
        if i["kind"] == "payload" and not i.get("ours"):
            ok(i.get("identified_by") == "content",
               "every third-party payload is recognised by content", i["id"])
    # THE VALUE IS NOT THE INVARIANT - the update button exists to change it, and pinning "0.5.0"
    # here meant a successful update turned this test red. What must hold is that these three are
    # read OUT OF THE BINARY and look like versions.
    import re as _re
    for want in ("kstuff", "pldmgr", "webkit-autoloader-installer"):
        hit = [i for i in items if i["id"] == want]
        ok(bool(hit), "%s is in the catalogue" % want)
        if not hit:
            continue
        ok(hit[0].get("version_from") == "file",
           "%s's version comes from inside the file" % want, hit[0].get("version_from"))
        ok(bool(_re.match(r"^\d+\.\d+", str(hit[0].get("version") or ""))),
           "...and reads like a version", hit[0].get("version"))

    # ---- THREE IMPLEMENTATIONS OF "SAME PAYLOAD" MUST AGREE -------------------------------------
    # The PC (payloads.proc_stem), the page (phbStem) and both consoles (pm_stem / p4_stem) each
    # reduce a filename to a comparable stem, and they must produce the same answer or the panel
    # reports a live payload as stopped. Observed exactly that way: the bundled copies were renamed
    # to stable ids and three running payloads went grey because only two of the three knew.
    web = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    ok("function phbStem(" in web, "the page has a stem function")
    for src, fn in ((os.path.join(ROOT, "ps5-app", "onconsole", "server.c"), "pm_stem"),
                    (os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c"), "p4_stem")):
        txt = io.open(src, encoding="utf-8").read()
        ok(("static void %s(" % fn) in txt, "%s exists in %s" % (fn, os.path.basename(src)))
        ok(("%s(want, wstem" % fn) in txt,
           "...and the load route matches with it, not with strcmp on the filename")
    for name, want in (("ftpsrv-ps5.elf", "ftpsrv"), ("ftpsrv.elf", "ftpsrv"),
                       ("pldmgr_v0.5.2.elf", "pldmgr"), ("pldmgr.elf", "pldmgr"),
                       ("webkit-autoloader-installer_v0.5.1.elf", "webkit-autoloader-installer"),
                       ("webkit-autoloader-installer.elf", "webkit-autoloader-installer")):
        ok(P.proc_stem(name) == want, "the PC reduces %s correctly" % name, P.proc_stem(name))

    # ---- THE BUNDLES NAME THEIR FILES BY ID, NOT BY VERSION -------------------------------------
    # Every .incbin path is a literal, so a versioned filename there breaks the build the first
    # time the update button takes a new release. That is not hypothetical: it happened.
    for hdr in (os.path.join(ROOT, "ps5-app", "onconsole", "payload_bundle.h"),
                os.path.join(ROOT, "ps4-app", "onconsole", "payload_bundle_ps4.h")):
        txt = io.open(hdr, encoding="utf-8").read()
        for m in re.findall(r'\.incbin \\"" file', txt) or [""]:
            pass
        bad = re.findall(r'INCBIN\([^,]+,\s*"payloads/([^"]+)"', txt)
        for f in bad:
            ok(not re.search(r"[-_]v?\d+\.\d", f),
               "an embedded payload path carries no version", "%s in %s" % (f, os.path.basename(hdr)))

    # ---- EVERY PAYLOAD NAMES ITS VERSION, AND SAYS WHERE IT GOT IT ------------------------------
    # Some carry one in the binary; the rest are proven by being byte-for-byte the size of an asset
    # in an upstream release, and that proof is recorded so it ships to a console with no PC and no
    # internet. A blank where a version belongs is the thing this replaced.
    kv = json.load(io.open(os.path.join(ROOT, "assets", "payloads", "known-versions.json"),
                           encoding="utf-8")).get("versions") or {}
    for i in items:
        if i["kind"] != "payload" or i.get("ours"):
            continue
        ok(bool(i.get("version")), "%s names a version" % i["id"], i.get("version"))
        ok(i.get("version_from") in ("file", "name", "release"),
           "...and says where it came from", "%s: %r" % (i["id"], i.get("version_from")))
        if i.get("version_from") == "release":
            ok(i.get("sha256") in kv,
               "a release-proven version is recorded by sha256", i["id"])
            ok(kv.get(i.get("sha256"), {}).get("version") == i.get("version"),
               "...and the record agrees with the catalogue", i["id"])
    # A recorded version belongs to ONE build of one file. Keyed by anything weaker and a different
    # build of the same project would inherit a version it never had.
    for sha in kv:
        ok(len(sha) == 64 and all(c in "0123456789abcdef" for c in sha),
           "known-versions is keyed by a full sha256", sha[:20])

    # ---- A UDP SERVICE IS SEEN BY TAKING ITS PORT, NOT BY CONNECTING TO IT ----------------------
    # nanodns listens on UDP 53 and answers nothing sent to it from the LAN - measured against its
    # own spoofing domains on both consoles - so a TCP connect and a DNS query both report "nothing
    # there" while it is running. The owner started it from this panel and the tile stayed grey.
    nd = [i for i in items if i["id"] == "nanodns"]
    ok(len(nd) == 2, "nanodns is catalogued for both consoles", "%d" % len(nd))
    for i in nd:
        ok(int(i.get("port") or 0) == 53, "...on port 53", i.get("port"))
        ok(i.get("probe") == "udp", "...and is probed by binding, not by connecting", i.get("probe"))
        ok(not i.get("autostart"), "...and is still never auto-started")
    for src, table in ((os.path.join(ROOT, "ps5-app", "onconsole", "server.c"), "PAYLOAD_BUNDLE"),
                       (os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c"), "PS4_PAYLOAD")):
        txt = io.open(src, encoding="utf-8").read()
        ok("udp_port_taken" in txt, "%s tests a UDP port by binding it" % os.path.basename(src))
        # SO_REUSEADDR would make the bind succeed beside the running server, and the test would
        # answer "free" for ever - the same permanently-wrong shape as the 9021 port mix-up.
        i = txt.find("static int udp_port_taken")
        ok(i > 0 and "SO_REUSEADDR" not in txt[i:i + 1400],
           "...without SO_REUSEADDR, which would make it always say free",
           os.path.basename(src))
    for hdr, ent in ((os.path.join(ROOT, "ps5-app", "onconsole", "payload_bundle.h"), "nanodns.elf"),
                     (os.path.join(ROOT, "ps4-app", "onconsole", "payload_bundle_ps4.h"), "nanodns.elf")):
        txt = io.open(hdr, encoding="utf-8").read()
        line = [l for l in txt.splitlines() if '"nanodns"' in l and ent in l]
        ok(bool(line), "nanodns is in %s" % os.path.basename(hdr))
        if line:
            ok(" 53," in line[0], "...at port 53", line[0].strip()[:70])

    # ---- qparam() TAKES THE PATH, NOT THE REQUEST -----------------------------------------------
    # Handing it the whole request text still finds a "?" - in the request LINE - so it parses a
    # value with " HTTP/1.1" stuck on the end and silently matches nothing. The console then
    # answered "this build does not carry that one" about a payload it was holding. Every other
    # caller in both files passes rawpath; this makes sure they keep doing that.
    for src in (os.path.join(ROOT, "ps5-app", "onconsole", "server.c"),
                os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c")):
        txt = io.open(src, encoding="utf-8").read()
        bad = re.findall(r"qparam\(\s*req\s*,", txt)
        ok(not bad, "no route reads a query parameter out of the raw request",
           "%s: %d call(s)" % (os.path.basename(src), len(bad)))

    # ---- A DEVICE WITHOUT THE FILES IS STILL PART OF THE FLEET ----------------------------------
    # The owner put the exe on a second PC and every tile read "not on this PC" with no status at
    # all - while the console next to it was carrying every payload and running half of them.
    # "This PC does not have it" and "nobody has it" are different answers.
    srv = io.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()
    ok("fleet_summary" in srv, "a companion advertises what it can hand over to peers")
    ok('"payloads": payloads_have' in srv, "...and it rides the federation reply")
    ok("peer_with(" in srv, "an action can find a peer that holds the file")
    ok("ask_peer(" in srv, "...and ask it to do the work")
    ok('it["from"]' in srv, "the list says WHERE each item would come from")
    for fn in ("fleet_summary", "peer_with", "ask_peer", "console_state"):
        ok(hasattr(P, fn), "payloads.%s exists" % fn)
    web = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    # The state line must not be gated on the file being local, or a payload running on the console
    # reads as "not on this PC" from every other machine.
    i = web.find("function phbPaintTile(")
    j = web.find("function phbUpFor(", i)
    seg = web[i:j if j > i else i + 4000]
    # THE FIRST MENTION OF EACH, not any mention: a version of this check that looked for the
    # ternary inside the payload branch still found it after a new `else if(!it.here)` was inserted
    # in FRONT of that branch - so it passed while the regression was present. Perturbed and
    # watched, which is how that was noticed.
    chain = seg[seg.find("var state;"):]
    a = chain.find("it.live===true")
    b = chain.find("!it.here")
    ok(a >= 0, "the tile decides a payload's running state")
    ok(b >= 0, "...and has a case for the bytes not being reachable")
    ok(a >= 0 and b >= 0 and a < b,
       "the running state is decided BEFORE the where-is-the-file case",
       "live at %d, not-here at %d" % (a, b))

    # ---- THE CATALOGUE IS SMALL ENOUGH TO SHIP EVERYWHERE --------------------------------------
    # It rides inside both ELFs through gen_web_bundle.py's assets/ allow-list.
    sz = os.path.getsize(CAT)
    ok(sz < 200 * 1024, "the catalogue is small enough to embed", "%d bytes" % sz)

    # ---- EVERY CURATED ENTRY IS REACHED --------------------------------------------------------
    # A curated key nothing matches is a port, an upstream and a warning that silently do nothing.
    cur = json.load(io.open(CURATED, encoding="utf-8"))
    for kind, table in (("payload", cur["payloads"]), ("homebrew", cur["homebrews"])):
        for key in table:
            hit = any(i["kind"] == kind and (i.get("title_id") == key or i["id"] == key)
                      for i in items)
            ok(hit, "curated entry is matched by something in the folder", "%s/%s" % (kind, key))

    # ---- ONE FLEET: EVERY DEVICE, INCLUDING FOR OUR OWN ARTIFACTS ------------------------------
    # The owner put the exe on a second PC and read "Not on this PC" on our own shop tile, in both
    # console sections, while the console beside it was plainly running it. Three separate defects
    # met there, and each one is pinned on its own because fixing any two still leaves a wrong tile.
    eng = io.open(os.path.join(ROOT, "companion", "payloads.py"), encoding="utf-8").read()
    srv = io.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()

    # (1) THE EXE CARRIES THE PS4 ELF, so a PC with no source folder still holds those bytes.
    ok("def bundled_ours(" in eng, "our own artifacts have a second home: inside the exe")
    ok('"ps4-elf"' in eng and "_MEIPASS" in eng,
       "...resolved from the frozen bundle server.py already ships")
    _NXT = chr(10) + "def "
    _lp = eng.split("def local_path(", 1)[1].split(_NXT, 1)[0]
    ok("bundled_ours(item)" in _lp, "...and local_path falls back to it for an `ours` item")
    ok("frozen" in eng.split("def bundled_ours(", 1)[1].split(_NXT, 1)[0],
       "...only when frozen, so a dev checkout cannot claim a build it has not made")

    # (2) WHAT WE ADVERTISE INCLUDES OUR OWN. The PS5 ELF is 34 MB and deliberately not bundled, so
    # a second PC can only ever get it from the PC that built it - which means offering it.
    _fs = eng.split("def fleet_summary(", 1)[1].split(_NXT, 1)[0]
    ok('it.get("ours")' not in _fs.split("out.append", 1)[0],
       "our own artifacts are advertised to peers, not skipped")
    ok("not local_path(cfg, it)" in _fs, "...and what we cannot reach is still not advertised")

    # (2b) A PEER STANDS IN FOR OUR OWN APP WHATEVER BUILD IT HOLDS. A second PC's baked catalogue
    # records the size our ELF was when ITS exe was built, so requiring a byte-exact match made the
    # tile say "nobody has this" about a file on the same LAN. For a third-party payload the strict
    # match must stay: a peer holding a different ftpsrv is not a substitute for the one described.
    sys.path.insert(0, os.path.join(ROOT, "companion"))
    import payloads as _P
    _peer = [{"online": True, "name": "OTHER",
              "payloads": [{"id": "pkg-mutant-shop", "platform": "PS5", "size": 45350920},
                           {"id": "ftpsrv", "platform": "PS5", "size": 999}]}]
    ok(_P.peer_with(_peer, {"id": "pkg-mutant-shop", "platform": "PS5",
                            "size": 34139632, "ours": True}) is not None,
       "a peer offering a DIFFERENT build of our own app still counts")
    ok(_P.peer_with(_peer, {"id": "ftpsrv", "platform": "PS5", "size": 123}) is None,
       "...but a third-party payload still needs the byte-exact build")
    ok(_P.peer_with(_peer, {"id": "pkg-mutant-shop", "platform": "PS4",
                            "size": 0, "ours": True}) is None,
       "...and the platform is still part of the match")

    # (3) THE FEDERATION FLAG MUST NOT REPORT A CONFIG FIELD THAT GATES NOTHING. It said False on a
    # fully-paired machine, which is what sent the owner looking for a pairing fault that was not
    # there. Auto-discovery is unconditional; `enabled` now answers "are we federated?".
    _pr = srv.split('if path == "/api/federation/peers":', 1)[1][:1200]
    ok('"enabled": bool(_known)' in _pr,
       "/api/federation/peers reports whether peers were actually found")
    ok('"discovery": True' in _pr, "...and says discovery is always on")

    # ---- A MISSING FOLDER IS MADE, NOT REPORTED ------------------------------------------------
    # The owner's complaint: "our app is not creating the folders it needs in the users pc ... our
    # app needs to be intelligent about everything and if it doesnt find them it creates them."
    import tempfile, shutil
    _d = tempfile.mkdtemp()
    try:
        _root = os.path.join(_d, "Mutant Payloads & HomeBrews")
        _r = _P.ensure_source_tree({"payloads": {"root": _root}})
        ok(_r.get("ok"), "the payloads tree is created when it is missing")
        # DERIVED, NOT HARD-CODED. scan() walks (Payloads|Homebrews) x PLATFORMS, so the folders
        # that get CREATED and the folders that get READ are checked against each other - a test
        # that re-listed the four names by hand would keep passing if scan() started looking
        # somewhere else.
        _want = set()
        for _top in ("Payloads", "Homebrews"):
            for _plat in _P.PLATFORMS:
                _want.add(os.path.join(_top, _plat))
        ok(set(_P.SOURCE_LAYOUT) == _want,
           "...and the layout it creates is the one scan() walks",
           "created %s" % sorted(_P.SOURCE_LAYOUT))
        for _sub in _want:
            ok(os.path.isdir(os.path.join(_root, _sub)), "...%s exists on disk" % _sub)

        # AN EMPTY FOLDER IS NOT AN EMPTY CATALOGUE. This is the regression creating the folder
        # introduced: a PC with no folder used to fall into the "not a directory" branch and serve
        # the BAKED catalogue, which is what puts the whole fleet's payloads in front of a second
        # PC that holds none of the files. Create the folder and that branch stops being taken.
        _cat, _sig = _P.live_catalog({"payloads": {"root": _root}}, os.path.join(ROOT, "web"))
        ok(len(_cat.get("items") or []) > 0,
           "a freshly created, EMPTY folder still shows the fleet's payloads",
           "%d items" % len(_cat.get("items") or []))
    finally:
        shutil.rmtree(_d, ignore_errors=True)

    # ---- THE PANEL HEADER IS IN THE OWNER'S ORDER ----------------------------------------------
    # Back, the console toggle, the title, the updates dropdown, the close.
    _ui = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    _hd = _ui.split('<header class="sethead phbhead">', 1)[1].split("</header>", 1)[0]
    _order = []
    for _id, _name in (("backPHB", "back"), ("phbTabs", "tabs"), ("phb_title", "title"),
                       ("phbUpdrop", "updates"), ("closePHB", "close")):
        _order.append((_hd.find(_id), _name))
    ok(all(i >= 0 for i, _n in _order), "every header control is present",
       ", ".join("%s@%d" % (n, i) for i, n in _order))
    ok(_order == sorted(_order), "the header row reads back, tabs, title, updates, close",
       " -> ".join(n for _i, n in sorted(_order)))

    # ---- THE DRAWER LEAVES #phb WHEN IT OPENS, SO ITS CSS MUST NOT BE SCOPED TO IT --------------
    # placeMenu() re-parents to <body> (any transform on an ancestor breaks position:fixed, and
    # .sheet's overflow-y:auto clips an absolute child - measured at 310px of a nine-row drawer).
    ok("placeMenu(b,$(\"#phbUpdHead\"))" in _ui,
       "the updates drawer is placed by placeMenu, not by CSS offsets")
    ok(".phbups{position:fixed" in _ui, "...and is position:fixed")
    ok("#phb .phbups" not in _ui,
       "...and no rule scopes it to #phb, which it is no longer inside when open")
    ok("phbUpdOpen(false)" in _ui.split("function _showPHB", 1)[1][:600],
       "closing the panel closes the drawer, which <body> would otherwise keep showing")

    # ---- TWO MESSAGES THE OWNER ASKED TO BE RID OF ---------------------------------------------
    ok("phb_upd_private" not in _ui,
       "the \"our releases are private for now\" message is gone, from the code and all 15 dictionaries")
    ok("phb_where" not in _ui,
       "the \"that folder is not on this PC\" message is gone - the folder is created instead")

    # ---- "INSTALLED" MEANS THE GAME'S DATA IS THERE ---------------------------------------------
    # The owner pressed Install on a homebrew the console did not have, was told "Done", and then
    # saw it listed as Installed. The title list was built from /user/appmeta, which is artwork: it
    # is written early, left behind by a failed install, and survives a database reset - the same
    # thing that once reported 53 titles installed on a console holding none of them.
    _ps5c = io.open(os.path.join(ROOT, "ps5-app", "onconsole", "server.c"), encoding="utf-8").read()
    _ps4c = io.open(os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c"), encoding="utf-8").read()
    for _name, _src in (("PS5", _ps5c), ("PS4", _ps4c)):
        _fn = _src.split("static int app_ids_json(char *out, size_t outsz) {", 1)[1].split("\nstatic ", 1)[0]
        ok("APPMETA_ROOTS" not in _fn,
           "%s: the installed-title list is not read from appmeta" % _name)
        # THE CALL, NOT A MENTION OF IT. Testing for the bare function name passed with the guard
        # deleted, because the comment above the guard names the function too - a check that a
        # comment can satisfy is not a check.
        ok("title_has_data(de->d_name)" in _fn or "installed_app_pkg(de->d_name)" in _fn,
           "%s: ...it requires the title's own app.pkg" % _name)
    # A MOUNTED TITLE HAS NO app.pkg AND NEVER WILL - nothing was installed, the container is
    # mounted in place. ShadowMount leaves mount.lnk for exactly that, and this server already
    # looks for it elsewhere. RetroArch is a folder app and would otherwise read "not installed"
    # while sitting on the home screen.
    _thd = _ps5c.split("static int title_has_data(", 1)[1].split(_NXT, 1)[0]
    ok("mount.lnk" in _thd, "PS5: a MOUNTED title counts as present too")
    ok("APPMETA_ROOTS" not in _ps5c and "APPMETA_ROOTS" not in _ps4c,
       "neither console still defines the appmeta roots")

    # ---- A RUNNING PROGRAM KNOWS ITS OWN VERSION ------------------------------------------------
    # The catalogue baked into an ELF records what the owner's folder held when that ELF was built,
    # so it is one release behind by construction: a console running 3.87.0 said "Running - 3.86.0"
    # and was offered an update it already had.
    for _name, _src in (("PS5", _ps5c), ("PS4", _ps4c)):
        _blk = _src.split('on_console\\":true,\\"platform\\":\\"%s' % _name, 1)[1][:1600]
        ok('shop_version' in _blk, "%s: /api/payloads reports the build that is answering" % _name)
    _ps4blk = _ps4c.split('on_console\\":true,\\"platform\\":\\"PS4', 1)[1][:1600]
    ok('\\"bundled\\":%d' in _ps4blk, "the PS4 reply carries `bundled`, like the PS5's")

    _ui = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    ok("PHB.consoleVer" in _ui, "the page keeps the console's own version")
    ok("it.ours && on && PHB.consoleVer" in _ui,
       "...and our own tile shows it while the shop is running")

    # ---- NOT HAVING A FILE IS A REASON TO FETCH IT ----------------------------------------------
    # A second PC answered "that file is not on this PC, so there is nothing to replace" for every
    # update it offered - live_catalog falls back to the baked catalogue when the folder is empty,
    # so a companion with no folder lists everything and could take none of it.
    _eng = io.open(os.path.join(ROOT, "companion", "payloads.py"), encoding="utf-8").read()
    _da = _eng.split("def download_asset(", 1)[1].split("\ndef ", 1)[0]
    ok('return False, "That file is not on this PC' not in _da,
       "an update no longer refuses because the file is absent")
    # ...and it lands IN THE OWNER'S FOLDER, which is the whole of the next check. Asserting the
    # name of a local variable here said nothing about where the bytes go; this drives the real
    # function and then looks on disk.
    _d4 = tempfile.mkdtemp()
    _real_bundled = _P.bundled_ours
    try:
        _root4 = os.path.join(_d4, "folder")
        _P.ensure_source_tree({"payloads": {"root": _root4}})
        _cfg4 = {"payloads": {"root": _root4}}
        # THE EXE'S OWN COPY, somewhere else entirely - this is PyInstaller's temp extraction
        # directory in the real thing, and it is what local_path() answers with when the folder has
        # no copy of its own.
        _meipass = os.path.join(_d4, "_MEI12345", "ps4-elf")
        os.makedirs(_meipass)
        _bund = os.path.join(_meipass, "PKG-MUTANT-SHOP-PS4.elf")
        with io.open(_bund, "wb") as f:
            f.write(b"ELF PKG MUTANT SHOP var APP_VERSION=\"3.90.0\" old")
        _P.bundled_ours = lambda it: (_bund if (it or {}).get("file") ==
                                      "PKG-MUTANT-SHOP-PS4.elf" else None)
        _newelf = os.path.join(_d4, "release-PKG-MUTANT-SHOP-PS4.elf")
        with io.open(_newelf, "wb") as f:
            f.write(b"ELF PKG MUTANT SHOP var APP_VERSION=\"3.99.9\" new")
        _item4 = {"id": "pkg-mutant-shop", "platform": "PS4", "ours": True, "kind": "payload",
                  "file": "PKG-MUTANT-SHOP-PS4.elf", "marker": "PKG MUTANT SHOP",
                  "title": "PKG MUTANT SHOP",
                  "path": "Payloads/PS4/pkg mutant shop/PKG-MUTANT-SHOP-PS4.elf"}
        _asset4 = {"name": "PKG-MUTANT-SHOP-PS4.elf", "size": os.path.getsize(_newelf),
                   "url": "file:///" + _newelf.replace(os.sep, "/")}
        _ok4, _msg4, _ver4 = _P.download_asset(_cfg4, _item4, _asset4)
        _want = os.path.join(_root4, "Payloads", "PS4", "pkg mutant shop",
                             "PKG-MUTANT-SHOP-PS4.elf")
        ok(_ok4, "an update with no copy in the folder still downloads", _msg4)
        ok(os.path.exists(_want), "...and the bytes land in the OWNER'S FOLDER", _want)
        # THE BUG THIS REPLACES: the download went next to the bundled copy - inside the running
        # exe's temp directory - so it reported the new version, vanished with the process, and the
        # panel offered the same update for ever.
        ok(not os.path.exists(os.path.join(_meipass, "PKG-MUTANT-SHOP-PS4.elf.part")),
           "...and nothing was written beside the copy inside the app")
        ok(io.open(_bund, "rb").read().endswith(b"old"),
           "...the copy the app carries is left exactly as it was")
        ok(_ver4 == "3.99.9", "...and the version reported is the one now in the folder", _ver4)

        # AND THE FOLDER'S FINGERPRINT MOVED, which is what the panel polls. The owner's report was
        # "it says Done and then shows the update again"; measured, the fingerprint had not changed
        # at all, because nothing inside the scanned tree had.
        ok(_P.folder_sig(_root4) != _P.folder_sig(os.path.join(_d4, "nope")),
           "...so the folder the panel watches has really changed")
    finally:
        _P.bundled_ours = _real_bundled
        shutil.rmtree(_d4, ignore_errors=True)

    # ---- A SHIPPED ENTRY MUST NOT CLAIM IT READ A FILE ------------------------------------------
    # The catalogue is baked by scanning the author's folder at build time, so its `version` is a
    # release behind by construction and `version_from` says "file" about a file on another
    # computer. On a PC that did not hold our PS4 payload that read as "3.89.0 -> v3.90.0" while
    # the app itself was already 3.90.0 and carrying 3.90.0 inside it.
    _d5 = tempfile.mkdtemp()
    _real_bundled5 = _P.bundled_ours
    try:
        _b5 = os.path.join(_d5, "PKG-MUTANT-SHOP-PS4.elf")
        with io.open(_b5, "wb") as f:
            f.write(b"ELF PKG MUTANT SHOP var APP_VERSION=\"3.90.0\"")
        _baked5 = {"items": [
            {"id": "pkg-mutant-shop", "platform": "PS4", "ours": True,
             "file": "PKG-MUTANT-SHOP-PS4.elf", "version": "3.89.0", "version_from": "file"},
            {"id": "ftpsrv", "platform": "PS5", "version": "1.16", "version_from": "file"}]}
        _P.bundled_ours = lambda it: (_b5 if (it or {}).get("file") ==
                                      "PKG-MUTANT-SHOP-PS4.elf" else None)
        _m5 = _P._merged(_baked5, {"items": []})
        _by = {(i["id"], i["platform"]): i for i in _m5["items"]}
        _mine = _by[("pkg-mutant-shop", "PS4")]
        ok(_mine["version"] == "3.90.0",
           "a shipped entry reports the version the app actually CARRIES", _mine["version"])
        ok(_mine["version_from"] == "bundled",
           "...and says the number came from the app, not from a file here",
           _mine["version_from"])
        ok(_by[("ftpsrv", "PS5")]["version_from"] == "shipped",
           "...and one with nothing to read says 'shipped' rather than 'file'",
           _by[("ftpsrv", "PS5")]["version_from"])
        # The live entry still wins outright - that is what _merged is for.
        _m6 = _P._merged(_baked5, {"items": [
            {"id": "pkg-mutant-shop", "platform": "PS4", "ours": True,
             "version": "4.0.0", "version_from": "file"}]})
        _mine6 = [i for i in _m6["items"] if i["id"] == "pkg-mutant-shop"][0]
        ok(_mine6["version"] == "4.0.0" and _mine6["version_from"] == "file",
           "...while a file that IS in the folder still describes itself")
    finally:
        _P.bundled_ours = _real_bundled5
        shutil.rmtree(_d5, ignore_errors=True)
    ok("had_old" in _da, "...and does not try to back up a file that was never there")

    # ---- A 200 {} IS NOT AN EMPTY LIST ----------------------------------------------------------
    # Neither console implements /api/payloads/updates, and an unknown route answers 200 {}. The
    # page read that as "checked, nothing found" and said everything was up to date.
    ok("r.items !== undefined" in _ui,
       "the page tells 'could not check' apart from 'nothing to update'")
    ok("phbSelfCheck" in _ui and "api.github.com" in _ui,
       "...and a console with no companion asks GitHub from the page, which has TLS")
    ok("phb_upd_cantcheck" in _ui, "...and says so when even that fails")

    # ---- "DONE" WAS SAID BEFORE A BYTE MOVED ----------------------------------------------------
    _do = _ui.split("function phbDo(", 1)[1].split("\nfunction ", 1)[0]
    ok('act==="install"' in _do and "phb_queued" in _do,
       "an install reports that it was queued, not that it is done")

    # ---- A STALE CONSOLE ID MUST NOT PIN A DEAD ADDRESS FOR EVER ------------------------------
    # The owner's PS4 moved address AND regenerated its id, so the saved pair matched nothing: the
    # id-first lookup found no candidate and the platform fallback was skipped because an id was
    # merely PRESENT. The companion asked a dead address for days while the console answered on
    # another one - and every PS4 tile in this panel read as unreachable because of it.
    _trk = srv.split("def track_consoles(", 1)[1].split(_NXT, 1)[0]
    ok("want_id not in seen_ids" in _trk,
       "an id nothing on the network reports is treated as no id at all")
    ok("len(cands) == 1" in _trk,
       "...while a second console of the same platform is still refused")

    # ---- ASKING WHAT IT WOULD DO MUST NOT DO IT ------------------------------------------------
    # /api/install grew dry_run after a test suite installed real games on the owner's console.
    # /api/payloads/install builds its own body for that lane and was not copying the flag across,
    # so the trap was reintroduced one layer up - a "dry run" here performed a real install.
    _act = srv.split("def _payloads_act(", 1)[1].split(_NXT, 1)[0]
    ok('"dry_run": body.get("dry_run")' in _act,
       "a homebrew install forwards dry_run to the install lane")

    # ---- A FINISHED JOB IS NOT A RUNNING ONE ---------------------------------------------------
    # `active` outlives a job so its outcome can still be shown. The accept gate knew that; the
    # engine-state route did not, so after the payload installed its own icon at boot the PS4
    # reported "busy" for ever and read as a wedged install lane.
    ok("static int job_running(void)" in _ps4c, "the PS4 has one definition of 'an install is running'")
    _st = _ps4c.split('if (!strcmp(path, "/api/engine/state"))', 1)[1][:1800]
    ok("job_running()" in _st,
       "...and the engine state uses it instead of the bare active flag")
    # A STATE THAT IS NEVER REFRESHED IS A STATE THAT NEVER ENDS. job_refresh() is what turns a
    # finished transfer into "installed"; without it this route reported busy for ever even after
    # the busy test itself was correct.
    ok("job_refresh()" in _st, "...after asking the console what the job is actually doing")

    # ---- THE URL THE CONSOLE IS HANDED MUST BE CLEAN -------------------------------------------
    # BGFT cannot fetch a URL with spaces or brackets in it, and percent-escaping does not help -
    # the PS5's local lane proved that and solved it with a token url. The PC-served lane was still
    # handing over the file's path verbatim, so one space in a folder the owner named "PKGI PS4"
    # was the whole difference between the homebrew that installed and the ones that did not.
    # Measured in the PS4's own log: register failed rc=0x80991400 ... uri=.../PKGI PS4/FPKGi....pkg
    import re as _re
    _bad = _re.compile(r"[^A-Za-z0-9._/-]")
    for _it in items:
        if _it.get("kind") != "homebrew":
            continue
        _k = _P.serve_key(_it)
        ok(not _bad.search(_k),
           "the serve key for %s has nothing a console installer cannot fetch" % _it["title"][:22],
           _k)
        if _it.get("shape") == "pkg":
            ok(_k.endswith(".pkg"),
               "...and a package's key ends in .pkg (a url that does not is refused outright)", _k)
    # Distinct per item, or two homebrews would serve each other's bytes.
    _keys = [_P.serve_key(i) for i in items if i.get("kind") == "homebrew"]
    ok(len(_keys) == len(set(_keys)), "every homebrew serves under its own key",
       "%d keys, %d distinct" % (len(_keys), len(set(_keys))))

    # ---- A FOLDER APP IS INSTALLABLE, NOT A REFUSAL --------------------------------------------
    # RetroArch is an app FOLDER. It goes to the drive ShadowMountPlus watches and mounts itself,
    # which is exactly what a game stored unpacked already does - so it takes that same lane. The
    # registry skipped folder-shaped items, so pressing Install could only ever answer "that is a
    # folder, not a package".
    _reg = srv.split("catalogue not registered", 1)[0]
    _tail = _reg[-1400:]
    ok('_it.get("shape") != "pkg"' not in _tail,
       "folder-shaped homebrews are registered, so the mount lane can find them")
    _act2 = srv.split("def _payloads_act(", 1)[1].split(_NXT, 1)[0]
    ok('"PS5"' in _act2 and "folder app" in _act2,
       "...and a folder is refused only on the console that cannot mount one")
    ok('"backup" if it.get("shape") != "pkg" else "base"' in _act2,
       "...and goes down the backup lane rather than the package lane")

    # ---- THE REPLY BUFFER MUST FIT ITS PARTS ---------------------------------------------------
    # snprintf truncates in silence. out[2600] held a reply whose parts total about 6.7 KB, so the
    # PS5 answered exactly 2599 bytes of invalid JSON the moment one more field was added - the same
    # shape of failure a 900-byte buffer once caused by cutting a 117-entry title list off at 72.
    # COMMENTS ARE NOT CODE, and this check read one. The note above the buffer explains the bug by
    # naming the old size, so the pattern found "out[2600]" in prose and measured that - which is
    # why shrinking the real buffer back to 2600 left this green. Strip comments first.
    import re as _re2
    _pl = _ps5c.split('if (!strcmp(path, "/api/payloads"))', 1)[1][:9000]
    _pl = _re2.sub(r"/\*.*?\*/", "", _pl, flags=_re2.S)
    # FIRST occurrence of each, not the last. `out` shares a line with esc2 so anchoring on "char "
    # missed it - and a dict comprehension over every match then picked up the NEXT route's own
    # out[340] instead, which made this check pass with the buffer shrunk back to 2600. A check that
    # survives its own perturbation is not a check.
    _sizes = {}
    for _m in _re2.finditer(r"\b(live|have|apps|have_p|esc2|out)\[(\d+)\]", _pl):
        _sizes.setdefault(_m.group(1), int(_m.group(2)))
    _need = sum(v for k, v in _sizes.items() if k != "out")
    ok(_sizes.get("out", 0) >= _need,
       "the /api/payloads reply buffer is at least as big as the parts it concatenates",
       "out=%d, parts=%d %s" % (_sizes.get("out", 0), _need, _sizes))
    ok("it was truncated" in _pl,
       "...and says so if it ever is, instead of sending half a document")

    # ---- ONE DOWNLOAD MUST NOT EMPTY THE SHELF -------------------------------------------------
    # A PC with no folder served the shipped catalogue, so all eighteen tiles appeared and could be
    # pressed. The folder then started being created automatically, so an empty scan became
    # possible - guarded by falling back to the shipped copy when the scan found NOTHING. Then the
    # owner took an update on that PC, one file landed in the new folder, the scan was no longer
    # empty, the guard no longer fired, and the whole panel became that single file: "Nothing here
    # for this console" on the other tab, and no payloads or homebrews anywhere.
    #
    # A folder holding SOME of the items is the ordinary case - it is what every PC looks like
    # between the first download and the last - so this builds exactly that and checks the panel
    # still describes the whole fleet.
    _d2 = tempfile.mkdtemp()
    try:
        _root2 = os.path.join(_d2, "Mutant Payloads & HomeBrews")
        _P.ensure_source_tree({"payloads": {"root": _root2}})
        _sub = os.path.join(_root2, "Payloads", "PS5", "pkg mutant shop")
        os.makedirs(_sub, exist_ok=True)
        _src = os.path.join(ROOT, "ps5-app", "onconsole", "PKG-MUTANT-SHOP.elf")
        if os.path.exists(_src):
            shutil.copy(_src, os.path.join(_sub, "PKG-MUTANT-SHOP.elf"))
        _c2, _s2 = _P.live_catalog({"payloads": {"root": _root2}}, os.path.join(ROOT, "web"))
        _i2 = _c2.get("items") or []
        ok(len(_i2) >= len(items),
           "a folder holding ONE file still shows the whole catalogue",
           "%d items with one file present, %d shipped" % (len(_i2), len(items)))
        _plats = {str(i.get("platform") or "").upper() for i in _i2}
        ok({"PS4", "PS5"} <= _plats,
           "...and both consoles still have something to show", "%s" % sorted(_plats))
        # The copy that IS there must be described by the file, not by the shipped record - that is
        # the whole reason for preferring the live entry.
        _ours5 = [i for i in _i2 if i.get("ours") and i.get("platform") == "PS5"]
        ok(bool(_ours5) and _ours5[0].get("version") == _P.ours_version(
            os.path.join(_sub, "PKG-MUTANT-SHOP.elf")),
           "...and the file that is present is described by the file")
    finally:
        shutil.rmtree(_d2, ignore_errors=True)

    # ---- THE APP CAN REPLACE ITS OWN EXE -------------------------------------------------------
    # The panel could update every payload and every homebrew and not the one file running it, so a
    # PC took an update, reported the new version, and stayed the old companion. This drives the
    # real swap: a file:// "release", the actual download, the size and MZ checks, both renames.
    _d3 = tempfile.mkdtemp()
    _real_running = _P.running_exe
    try:
        _exe = os.path.join(_d3, "PKG-MUTANT-SHOP.exe")
        with io.open(_exe, "wb") as f:
            f.write(b"MZ" + b"old build" * 64)
        _newbytes = b"MZ" + b"new build" * 64
        _src3 = os.path.join(_d3, "release.exe")
        with io.open(_src3, "wb") as f:
            f.write(_newbytes)

        # EACH CASE IS ITS OWN RELEASE. update_self() replaces the exe once per release tag - our
        # own entry appears once per console, so "Update all" calls it twice for one release and the
        # second call must not try to move the running image again. Reusing one tag here would make
        # the download-integrity cases below skip the download entirely and pass for the wrong
        # reason, which is how this file found that guard's edge in the first place.
        _tagn = [0]

        def _rel_for(path, size, tag=None):
            if tag is None:
                _tagn[0] += 1
                tag = "v9.9.%d" % _tagn[0]
            return {"tag": tag, "assets": [
                {"name": "PKG-MUTANT-SHOP.exe",
                 "url": "file:///" + path.replace(os.sep, "/"), "size": size}]}

        _P.running_exe = lambda: _exe
        _ok3, _msg3 = _P.update_self({}, _rel_for(_src3, len(_newbytes)))
        ok(_ok3, "the app replaces its own exe", _msg3)
        ok(io.open(_exe, "rb").read() == _newbytes,
           "...and the file under the app's own name IS the new build")
        # The build that was running has to survive under a name of its own: it is the file this
        # process is still executing from, so deleting it at swap time would pull the floor out.
        ok(os.path.exists(_exe + ".old"), "...and the running build is kept aside, not deleted")
        ok(not os.path.exists(_exe + ".new"), "...and no half-finished download is left behind")
        _P.sweep_old_exe()
        ok(not os.path.exists(_exe + ".old"), "...and the next start clears the old build away")

        # A TRUNCATED OR WRONG DOWNLOAD MUST NOT BECOME THE APP. Both of these used to be the way
        # an update lane destroys a working install, and the running build has to be untouched
        # afterwards - not restored, never moved.
        _short = os.path.join(_d3, "short.exe")
        with io.open(_short, "wb") as f:
            f.write(_newbytes[:20])
        _ok4, _msg4 = _P.update_self({}, _rel_for(_short, len(_newbytes)))
        ok(not _ok4 and io.open(_exe, "rb").read() == _newbytes,
           "a short download is refused and the app is left alone", _msg4)
        _html = os.path.join(_d3, "notanexe.bin")
        with io.open(_html, "wb") as f:
            f.write(b"<html>rate limited</html>")
        _ok5, _msg5 = _P.update_self({}, _rel_for(_html, os.path.getsize(_html)))
        ok(not _ok5 and io.open(_exe, "rb").read() == _newbytes,
           "something that is not a Windows program is refused", _msg5)
        ok(not os.path.exists(_exe + ".new"),
           "...and neither refusal leaves a stray file next to the app")

        # Running from source, there is no exe to replace and it must say so rather than guess.
        _P.running_exe = lambda: None
        _ok6, _ = _P.update_self({}, _rel_for(_src3, len(_newbytes)))
        ok(not _ok6, "running from source, there is nothing to replace")

        # TAKING BOTH OF OUR TILES IS ONE UPDATE, NOT TWO. Our own entry appears once per console,
        # so "Update all" calls this twice within a second. The second call used to find the running
        # image already renamed aside by the first and fail trying to move it again - so a PC that
        # had just updated itself perfectly told the owner "Could not set the running app aside
        # (PermissionError)". Measured on the owner's second PC, PS5 row then PS4 row.
        _P.running_exe = lambda: _exe
        _rel7 = _rel_for(_src3, len(_newbytes), tag="v9.9.100")
        _P._swapped_to.clear()
        _okA, _msgA = _P.update_self({}, _rel7)
        # AND THE SLOT THE FIRST SWAP USED IS NOT AVAILABLE TWICE. In the real thing <exe>.old is
        # the image this process is executing from, so it can be neither deleted nor replaced until
        # the process ends - which is what made the second call fail. A directory in its place is
        # the same refusal from the filesystem, and it is reproducible on any machine.
        _oldp = _exe + ".old"
        if os.path.exists(_oldp):
            os.remove(_oldp)
        os.makedirs(_oldp)
        _okB, _msgB = _P.update_self({}, _rel7)
        shutil.rmtree(_oldp, ignore_errors=True)
        ok(_okA and _okB, "taking the second of our two tiles is not a failure",
           "first=%r second=%r" % (_msgA, _msgB))
        ok("ermission" not in _msgB and "aside" not in _msgB,
           "...and it does not report a permission error for work already done", _msgB)
        ok(io.open(_exe, "rb").read() == _newbytes,
           "...and the app on disk is still the new build")
        # A DIFFERENT release must still be taken: the guard is per release, not for ever.
        _P._swapped_to.clear()
        _newer = os.path.join(_d3, "newer.exe")
        with io.open(_newer, "wb") as f:
            f.write(b"MZ" + b"newer build" * 64)
        _okC, _ = _P.update_self({}, {"tag": "v9.9.10", "assets": [
            {"name": "PKG-MUTANT-SHOP.exe",
             "url": "file:///" + _newer.replace(os.sep, "/"),
             "size": os.path.getsize(_newer)}]})
        ok(_okC and io.open(_exe, "rb").read().endswith(b"newer build"),
           "...while the next release is still installed")
    finally:
        _P.running_exe = _real_running
        shutil.rmtree(_d3, ignore_errors=True)

    # running_exe() IS THE GATE on all of the above, so it has to be honest about this process:
    # under the test runner nothing is frozen, and a stub that returned a path anyway would let the
    # swap loose on whatever sys.executable happens to be - the python interpreter.
    ok(_P.running_exe() is None, "running_exe() names no exe when the build is not frozen",
       "%r" % (_P.running_exe(),))

    # ---- SIZE CANNOT VOUCH FOR OUR OWN ARTIFACT ------------------------------------------------
    # Every "already on the console" shortcut in the engine compares lengths. A build of this app
    # differs from the last by a version string of the same width, so the length does not move -
    # three consecutive releases of the PS4 payload are all 9,522,528 bytes. The shortcut therefore
    # reported a freshly updated payload as already there, skipped the send, and the Run shortcut
    # started the console's OLD copy while the panel showed the new number.
    ok(_P.size_can_vouch({"ours": False}) is True,
       "a third-party payload may still be vouched for by its size")
    ok(_P.size_can_vouch({"ours": True}) is False,
       "our own artifact may not - its length does not change between builds")
    ok(_P.size_can_vouch({}) is True, "an item that says nothing is not ours")
    # ...and the three shortcuts really consult it, rather than each keeping its own rule.
    _eng2 = io.open(os.path.join(ROOT, "companion", "payloads.py"), encoding="utf-8").read()
    ok(_eng2.count("size_can_vouch(") >= 4,
       "every size shortcut asks size_can_vouch()", "%d use(s)" % _eng2.count("size_can_vouch("))
    for _fn, _why in (("console_copy_is_current", "the console's own copy"),
                      ("send_ps5", "the payload upload"),
                      ("seed_homebrew", "the homebrew seed")):
        if ("def %s(" % _fn) in _eng2:
            _body = _eng2.split("def %s(" % _fn, 1)[1].split("\ndef ", 1)[0]
            ok("size_can_vouch(" in _body, "%s asks before trusting a size" % _why)

    # The evidence itself, so this cannot quietly stop being true: our own catalogue entries record
    # the PS4 and PS5 artifacts, and a build that changed one byte of version would otherwise look
    # like a different file.
    _ours = [i for i in items if i.get("ours")]
    ok(len(_ours) == 2, "the catalogue carries both of our artifacts", "%d" % len(_ours))

    # ---- THE PANEL HAS THREE ANSWERS FOR "INSTALLED", NOT TWO ----------------------------------
    # installed arrives true / false / null, and null means the reply is not about that console.
    # Printing "Not installed" for null asserts a fact about a console nobody asked.
    _ui2 = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    ok('(it.installed===false)?t("phb_notinstalled")' in _ui2,
       "the tile prints Not installed only when the console actually said so")
    ok('t("phb_unkinst")' in _ui2, "...and an unknown state has a word of its own")
    ok('(it.installed===true)?t("phb_installed")' in _ui2,
       "...and Installed only when the console actually said so")
    # A press on an unknown must ASK, not install over the top.
    ok('it.kind==="payload" ? it.live===true : it.installed!==false' in _ui2,
       "a press on a homebrew whose state is unknown opens the confirm row")
    # The badge must be re-derived from the server, never assumed.
    ok("PHB.ups=PHB.ups.filter" not in _ui2,
       "taking an update no longer empties the update row on trust alone")
    _upone = _ui2.split("function phbUpOne(", 1)[1].split("\nfunction ", 1)[0]
    ok("phbCheck(true)" in _upone, "...it re-asks the server what is still outstanding")

    if fails:
        print("test_payloads: FAIL")
        for f in fails:
            print("   %s" % f)
        return 1
    print("test_payloads: OK (%d checks, %d catalogue entries)" % (n, len(items)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

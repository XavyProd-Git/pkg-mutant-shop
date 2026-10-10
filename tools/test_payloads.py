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
    import io as _io2
    cat = json.load(io.open(CAT, encoding="utf-8"))
    items = cat["items"]
    ok(len(items) >= 10, "the catalogue has entries", "%d" % len(items))

    # ---- identity ------------------------------------------------------------------------------
    for it in items:
        ok(it.get("id"), "every item has an id", str(it)[:80])
        ok(it.get("platform") in ("PS4", "PS5"), "every item names a platform", it.get("id"))
        ok(it.get("kind") in ("payload", "homebrew"), "every item names a kind", it.get("id"))
        ok(it.get("shape") in ("elf", "pkg", "folder", "zip"), "every item names a shape",
           it.get("id"))
        if it.get("shape") == "zip":
            ok(it.get("kind") == "homebrew", "only a homebrew is ever an archive", it.get("id"))

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
        # A HELPER IS NOT RECOGNISED BY CONTENT BECAUSE THERE IS NOTHING TO RECOGNISE IT BY. This
        # rule is about the payloads the project curates: each has a marker recorded for it, so a
        # renamed file keeps its port, its upstream and its jailbreak warning. A helper ELF shipped
        # beside an emulator has no marker, no curated entry and no upstream - identifying it by
        # anything other than the folder it was downloaded into would mean matching it against
        # some other project's marker, which is worse than saying "folder".
        if i["kind"] == "payload" and not i.get("ours") and not i.get("helper_for"):
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
        if i.get("helper_for"):
            # A HELPER HAS NO UPSTREAM OF ITS OWN, so it has no version to name - see the block
            # below, which is the rule that does apply to it.
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

    # ---- A JAILBROKEN PS4 WITH NO PAYLOAD RUNNING IS STILL THE CONSOLE -------------------------
    # The owner's report: on a new network the app never finds the PS4 and the address has to be
    # typed in. Three causes, all of them in the finder.
    #
    #   * discover_ps5 only accepted a host our own shop answered on, or one with Payload Manager
    #     on 8084. A PS4 that has just booted has neither - our payload does not survive a reboot -
    #     so it was invisible.
    #   * track_consoles returned before sweeping whenever every CONFIGURED console answered, so
    #     once the PS5 was adopted the PS4 was never looked for again.
    #   * the first fingerprint tried was GoldHEN's payload loader on 9090. Measured on the owner's
    #     console: it refused every connection for five probes in a row while FTP answered
    #     "220 GoldHEN FTP server v2.2" every time. A signal that is sometimes absent is not one.
    #
    # Driven against real sockets so no console needs to be awake.
    import socket as _sk
    import threading as _th
    import server as _S

    def _greeter(text, hold=0.4):
        """A socket that greets like an FTP server and then goes away. Returns its port."""
        srv = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
        srv.setsockopt(_sk.SOL_SOCKET, _sk.SO_REUSEADDR, 1)
        srv.bind(("127.0.0.1", 0))
        srv.listen(4)
        port = srv.getsockname()[1]

        def run():
            srv.settimeout(6)
            try:
                c, _ = srv.accept()
            except Exception:
                try:
                    srv.close()
                except Exception:
                    pass
                return
            try:
                if text:
                    c.sendall(text.encode("ascii"))
                import time as _t
                _t.sleep(hold)
            except Exception:
                pass
            finally:
                try:
                    c.close()
                except Exception:
                    pass
                try:
                    srv.close()
                except Exception:
                    pass

        t = _th.Thread(target=run)
        t.daemon = True
        t.start()
        return port

    # The companion's own source, read once: two of the assertions below are about what the code
    # DOES NOT do, and only the text can show that.
    _srvS = io.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()
    _srvD = _srvS
    # A port nothing is listening on, for the "no console here" answer.
    _dead = _sk.socket(_sk.AF_INET, _sk.SOCK_STREAM)
    _dead.bind(("127.0.0.1", 0))
    _deadport = _dead.getsockname()[1]
    _dead.close()

    _gh = _greeter("220 GoldHEN FTP server v2.2\r\n")
    ok(_S._identifies_as_ps4_jailbreak("127.0.0.1", timeout=2.0, ftp_ports=(_gh,)) is True,
       "a greeting that names GoldHEN identifies a jailbroken PS4")

    _other = _greeter("220 ProFTPD 1.3.5 Server ready\r\n")
    ok(_S._identifies_as_ps4_jailbreak("127.0.0.1", timeout=2.0, ftp_ports=(_other,)) is False,
       "...and somebody else's FTP server does not",
       "an open FTP port is not a console - that is why the NAME is required")

    ok(_S._identifies_as_ps4_jailbreak("127.0.0.1", timeout=1.0, ftp_ports=(_deadport,)) is False,
       "...and a host with nothing listening is not a console either")

    # THE FINGERPRINT MUST NOT TOUCH THE LOADER, which is the opposite of what it used to do. It
    # took a loader_port and asked it for /status as a second opinion; GoldHEN's BinLoader stops
    # listening when a connection brings it anything that is not an ELF, and it is the only lane
    # by which a new PS4 payload gets loaded. Read out of the source, because a signature with no
    # parameter still proves nothing about what the body does.
    _fsrc = _srvS.split("def _identifies_as_ps4_jailbreak(", 1)[1].split(chr(10) + "def ", 1)[0]
    ok("loader_port" not in _fsrc.split(chr(34) * 3, 2)[-1],
       "the jailbreak fingerprint never opens GoldHEN's loader",
       "it is the one lane that loads a new PS4 build, and a non-ELF connection kills it")
    # THE CODE, NOT THE COMMENTS. The body deliberately explains why that port is left alone -
    # "2121 answered five times out of five while 9090 refused five out of five" - and a comment
    # recording the measurement that chose the greeting is the opposite of a defect.
    _fcode = chr(10).join(ln.split("#", 1)[0]
                          for ln in _fsrc.split(chr(34) * 3, 2)[-1].splitlines())
    ok("9090" not in _fcode,
       "...and no line of its code names that port either",
       "the comments may, and should")

    # THE SWEEP MUST ASK. These are the two conditions that made the console invisible; both are
    # read out of the function rather than trusted to a comment.
    _dsrc = _srvD.split("def discover_ps5(", 1)[1].split(chr(10) + "def ", 1)[0]
    # THE PORTS TUPLE ITSELF, not the function's prose. Either way round, this has to read the
    # list rather than the comment above it, because the comment explains whichever answer is
    # current.
    #
    # AND THE ANSWER IS NOW THE OPPOSITE ONE. This asserted that the sweep PROBES 9090 - it was
    # there to find a PS4 that had just booted, since our payload does not survive a reboot. What
    # it actually did was open GoldHEN's BinLoader and close it again, 254 times, which stops that
    # loader listening; and the loader is the only way a new PS4 build gets loaded. The measured
    # alternative is better anyway: GoldHEN's FTP greeting answered five probes out of five on the
    # owner's console while 9090 refused five out of five, and 2121 and 1337 are both in the list
    # below. companion/payloads.py's own module header has said "never TCP-probe a PS4's :9090"
    # all along; two other files disagreed with it.
    _plist = _dsrc.split("ports = tuple(", 1)[1].split("))", 1)[0] if "ports = tuple(" in _dsrc else ""
    ok("9090" not in _plist, "the sweep does NOT probe GoldHEN's loader port",
       "ports = %s" % _plist.strip())
    ok("1337" in _plist and "2121" in _plist,
       "...and sweeps both FTP ports instead, which is how a payload-less PS4 is found",
       "the greeting names GoldHEN; the port alone proves nothing")
    ok("_identifies_as_ps4_jailbreak(" in _dsrc,
       "...and asks whether the host announces a PS4 jailbreak when our shop cannot be asked")
    ok('"loader"' in _dsrc,
       "...and reports which hosts were found that way, so a caller can tell a console that needs "
       "its payload from one that is ready")

    _tsrc = _srvD.split("def track_consoles(", 1)[1].split(chr(10) + "def ", 1)[0]
    # THE CONDITION, not merely the name. Reverting just the `if` left the two assignments above
    # it in place, so looking for the name anywhere in the function passed while the early return
    # was back.
    _guard = [ln for ln in _tsrc.splitlines() if ln.strip().startswith("if (not silent")
              or ln.strip().startswith("if not silent")]
    ok(bool(_guard), "the finder still has its cheap-half guard")
    ok(_guard and "_missing_plat" in _guard[0],
       "the finder keeps looking while a platform has no entry at all",
       "guard reads: %s" % (_guard[0].strip() if _guard else "<gone>"))
    ok("jb4" in _tsrc,
       "...and can follow or adopt a PS4 that has no payload running")
    ok("dict(seen).get(" in _tsrc,
       "...without assuming every match was a host our shop answered on",
       "dict(seen)[ip] raises for a PS4 matched by its jailbreak alone")

    # ---- `have` IS A FACT ABOUT THIS MACHINE, NOT THE ONE THAT BUILT THE APP --------------------
    # MEASURED on the owner's second PC: three PS4 homebrews reported have=true and from="peer" in
    # the same reply - "the file is in this folder" and "the file is on the other machine", about
    # one file. That PC held none of them; have=true came out of the baked catalogue, which is
    # generated by scanning the AUTHOR's folder and shipped inside all three artifacts.
    #
    # It is not cosmetic. The Download drawer keys "you already have this" on `have`, so it hid
    # all three, while `here` - computed honestly from the filesystem - made the tile say "Not on
    # this PC". When the other PC left the network there was nothing left but a refusal, on tiles
    # for programs the console already had installed. That is what the owner saw.
    #
    # Driven against a real folder holding exactly ONE of the catalogue's files, which is what
    # every PC looks like between its first download and its last.
    import shutil as _sh7
    import tempfile as _tf7
    _root7 = _tf7.mkdtemp(prefix="pms-gate-secondpc-")
    try:
        _cfg7 = {"payloads": {"root": _root7}}
        ok(P.source_root(_cfg7) == _root7, "a second PC's folder can be pointed somewhere else")
        # READ FRESH, AND PICKED DETERMINISTICALLY. `items` was loaded at the top of this run,
        # and ready_check regenerates the catalogue from the owner's live folder before getting
        # here - so a download finishing mid-run could leave the list this gate reasons about out
        # of step with the one on disk. This gate failed exactly once that way and passed six
        # times after; a check that depends on what the owner is doing is not a check.
        _items7 = json.load(io.open(CAT, encoding="utf-8"))["items"]
        _cand7 = sorted([_i7 for _i7 in _items7
                         if _i7.get("kind") == "homebrew" and _i7.get("shape") == "pkg"
                         and _i7.get("path")],
                        key=lambda _x: _x["path"])
        _held7 = _cand7[0] if _cand7 else None
        ok(_held7 is not None, "the catalogue has a package to stand in for the one file we hold")
        _hp7 = os.path.join(_root7, _held7["path"].replace("/", os.sep))
        os.makedirs(os.path.dirname(_hp7))
        io.open(_hp7, "wb").write(b"\0" * 2048)

        _m7 = P._merged({"items": [dict(_x) for _x in _items7]}, {"items": []}, _cfg7)["items"]
        _lies7, _rows7 = [], 0
        for _r7 in _m7:
            if _r7.get("kind") != "homebrew" or not _r7.get("path"):
                continue
            _rows7 += 1
            _real7 = os.path.exists(os.path.join(_root7, _r7["path"].replace("/", os.sep)))
            if bool(_r7.get("have")) != _real7:
                _lies7.append("%s says have=%s, the file is %s"
                              % (_r7.get("title"), bool(_r7.get("have")),
                                 "there" if _real7 else "NOT there"))
        ok(_rows7 >= 4, "enough shipped entries to be worth checking", "%d row(s)" % _rows7)
        ok(not _lies7,
           "no shipped entry claims this machine holds a file it does not",
           "; ".join(_lies7[:4]))
        # ...AND NOT BY ANSWERING FALSE TO EVERYTHING, which would pass a check that only looked
        # for the lie.
        _mine7 = [_x for _x in _m7 if _x.get("path") == _held7.get("path")]
        ok(bool(_mine7), "the one file this machine holds is still in the catalogue")
        ok(_mine7 and bool(_mine7[0].get("have")),
           "...and still reports have=true, so this is not 'always false'")
    finally:
        _sh7.rmtree(_root7, ignore_errors=True)

    # ---- READING THE CATALOGUE MUST NOT CHANGE IT -----------------------------------------------
    # catalog() memoises the parsed shipped JSON and hands THE SAME nested dicts to every caller.
    # _merged() used to write `have` into them, so the second merge saw a catalogue whose entries
    # all read have=False, stopped collapsing the live offers onto them, and returned 36 rows for
    # a 29-row catalogue - seven programs twice over, DolphinPS4 among them. That is the pair the
    # owner was looking at: an offer row in the drawer and a tile reading "Not on this PC".
    #
    # The gate written for `have` an hour earlier could not see any of it, because it hands
    # _merged a freshly copied list and calls it once. This calls it twice on ONE shared object,
    # which is what the server does: the second merge is what a finished download triggers.
    _shared9 = {"items": [dict(_x) for _x in _items7]}
    _before9 = [bool(_x.get("have")) for _x in _shared9["items"]]
    _live9 = {"items": []}
    _r9a = P._merged(_shared9, _live9, None)
    _r9b = P._merged(_shared9, _live9, None)
    ok(len(_r9a["items"]) == len(_r9b["items"]),
       "reading the catalogue twice returns the same number of rows",
       "%d then %d" % (len(_r9a["items"]), len(_r9b["items"])))
    ok([bool(_x.get("have")) for _x in _shared9["items"]] == _before9,
       "...and leaves the shipped catalogue's own `have` untouched",
       "a merge that writes into it makes every later read disagree with the first")
    # AND NO PROGRAM MAY APPEAR TWICE. Keyed the way the panel keys a tile.
    _seen9 = {}
    _dupes9 = []
    for _x in _r9b["items"]:
        _k9 = (str(_x.get("platform") or "").upper(), _x.get("id"))
        _seen9[_k9] = _seen9.get(_k9, 0) + 1
        if _seen9[_k9] == 2:
            _dupes9.append("%s/%s" % _k9)
    ok(not _dupes9, "...and no program is listed twice", ", ".join(_dupes9[:6]))

    # ---- A FILE THAT ARRIVES AFTER THE SCAN IS STILL ON THIS PC ---------------------------------
    # The owner downloaded DolphinPS4 from the console, it landed in the answering PC's folder, and
    # pressing Install answered "DolphinPS4 is not on this PC." srv.library.file_registry is built
    # inside Library.scan(), and a scan is a moment: every file this panel downloads arrives after
    # it and nothing in the download lane triggers another, so the install route was reading a
    # snapshot as if it were the filesystem.
    _root8 = _tf7.mkdtemp(prefix="pms-gate-afterscan-")
    try:
        _cfg8 = {"payloads": {"root": _root8}}
        _it8 = {"id": "DLPH00010", "platform": "PS4", "kind": "homebrew", "shape": "pkg",
                "title_id": "DLPH00010", "file": "DolphinPS4-v04.00.pkg", "size": 0,
                "path": "Homebrews/PS4/DolphinPS4/DolphinPS4-v04.00.pkg"}
        _reg8, _sz8 = {}, {}
        ok(P.register_local(_reg8, _sz8, _cfg8, _it8) == "",
           "a file that is genuinely not here registers nothing", "%r" % (_reg8,))
        ok(not _reg8, "...and leaves the registry alone")
        _p8 = os.path.join(_root8, _it8["path"].replace("/", os.sep))
        os.makedirs(os.path.dirname(_p8))
        io.open(_p8, "wb").write(b"x" * 321)
        _k8 = P.register_local(_reg8, _sz8, _cfg8, _it8)
        ok(bool(_k8), "a file that arrived after the scan registers itself", "%r" % _k8)
        ok(_reg8.get(_k8) == _p8, "...under the path it is really at", "%r" % _reg8.get(_k8))
        ok(_sz8.get(_k8) == 321, "...with its real size", "%r" % _sz8.get(_k8))
        ok(_k8 == P.serve_key(_it8), "...and under the key the install lane will look up")
    finally:
        _sh7.rmtree(_root8, ignore_errors=True)

    # AND THE REFUSAL MUST NOT BE REACHABLE WITHOUT ASKING FIRST. Found by shape, not by a list:
    # every place in the payload routes that says "is not on this PC" has to sit in a block that
    # consulted the filesystem. A fifth one gets caught the day it is written.
    _srvA = io.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()
    _actsrc = _srvA.split("def _payloads_act(", 1)[1].split(chr(10) + "    def ", 1)[0]
    # ORDER, NOT DISTANCE. A character window is the wrong measure - it was satisfied by an
    # unrelated local_path() elsewhere in this long method, and it then broke the moment a second
    # legitimate check (the console's own copy) was added between the guard and the refusal. What
    # the rule actually says is: before this route tells the reader a file is nowhere, it must
    # have asked both places it could be.
    _ins = _actsrc.split('if what == "install":', 1)
    ok(len(_ins) == 2, "the install branch is still where this rule applies")
    _insb = _ins[1]
    ok('"%s is not on this PC."' in _insb,
       "the refusal this is about is still in the install branch")
    _at = _insb.index('"%s is not on this PC."')
    _before = _insb[:_at]
    ok("register_local(" in _before,
       "the install branch asks the filesystem before saying a file is nowhere",
       "nothing registers a local file before that refusal")
    ok("kept_match(" in _before,
       "...and asks whether the console is holding it",
       "the panel proves a console copy exists for the tile and then refuses anyway")

    # ---- A HELPER ELF IS NOT CATALOGUED AT ALL, WHICH IS HOW 3.96.0 HAD IT ------------------
    # 688e679 catalogued every .elf found under Homebrews/ as kind:"payload", to give the two
    # helpers an emulator will not start without a Run button. It cost four tiles in the Payloads
    # grid named after other people's projects, and - because a helper has no repo - four rows in
    # the Download drawer that nothing could ever fetch, which is four of the five that badge
    # counted on a console with no PC. The owner's words: "Emulators, autologs and installers of
    # emulators shouldnt go there."
    #
    # NOTHING IS LOST, and that is the part worth asserting: send_extras still fetches each helper
    # from the curated `extras` list and sends it with its emulator, into the emulator's own folder.
    _helpers = [i for i in items if i.get("helper_for")]
    ok(not _helpers, "no helper ELF is catalogued as a payload of its own",
       "%d still are: %s" % (len(_helpers), ", ".join(h["id"] for h in _helpers)))
    _psrc = io.open(os.path.join(ROOT, "companion", "payloads.py"), encoding="utf-8").read()
    ok('if kind == "homebrew" and low.endswith(".elf"):' in _psrc
       and "A HELPER IS NOT CATALOGUED" in _psrc,
       "...and the branch that used to catalogue them says why it does not")
    ok("def send_extras" in _psrc,
       "...and the extras lane that actually delivers them is still there")

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
    # ASKED OF THE REAL RESOLVER, not of a copy of its rules. There are four ways a curated entry
    # is reached - its key as a title id, its key as an item id, its key as the slug of the folder
    # name, and the title id it declares - and this check used to know two of them. Three entries
    # then went unreachable-looking the moment their items started being identified by title id
    # instead of folder slug, while they were in fact matching perfectly.
    cur = json.load(io.open(CURATED, encoding="utf-8"))
    _scanned = P.scan(P.source_root({}), cur)
    P.apply_curated(_scanned, cur)
    _used = set((i["kind"], i.get("curated_key")) for i in _scanned if i.get("curated_key"))
    _offered = set((i["kind"], i.get("id")) for i in items if i.get("have") is False)
    for kind, table in (("payload", cur["payloads"]), ("homebrew", cur["homebrews"])):
        for key, ent in table.items():
            if (kind, key) in _used:
                continue
            # An entry for something NOT downloaded yet is reached by available_items() instead,
            # and that is the whole point of it - so it counts as reached, by the other lane.
            ident = P.slug((ent or {}).get("title") or key) or key
            ok((kind, ident) in _offered or (kind, key) in _offered,
               "curated entry is reached, by the scan or by the offer list",
               "%s/%s" % (kind, key))

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
    # One open/close serves both drawers now, so the head id is an argument rather than a literal.
    ok('placeMenu(b,$("#"+headId),true)' in _ui,
       "a drawer is placed by placeMenu, not by CSS offsets",
       "...and with below=true, so a dropdown drops DOWN - its pill is in the panel header and "
       "the default preference is upward, which flew the list to the top of the screen")
    ok('phbDropOpen("phbUpdrop","phbUps","phbUpdHead"' in _ui and
       'phbDropOpen("phbGetdrop","phbGets","phbGetHead"' in _ui,
       "...and both drawers pass their own head to it")
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

    # ---- AND THE PAGE MUST OFFER IT -------------------------------------------------------------
    # THE GATE ABOVE WAS GREEN WHILE NONE OF THEM COULD BE PRESSED. It asks the server, and the
    # server was right all along: the refusal was in the page, where phbPress() toasted and
    # returned for any homebrew whose shape was not "pkg" - all six PS5 folder apps, every
    # emulator in the panel. Gating one side of a two-sided decision is exactly how that survived
    # a release.
    #
    # So this one RUNS the page's decision instead of reading it. phbActs() is lifted out of
    # web/index.html and executed against items built to be each case, on both lanes.
    import json as _json4
    import subprocess as _sub4
    import tempfile as _tmp4

    def _jsfn(src, name):
        """The text of one function, bounded by its own braces rather than by a blank line."""
        i = src.index("function %s(" % name)
        d, started = 0, False
        for j in range(i, len(src)):
            if src[j] == "{":
                d += 1
                started = True
            elif src[j] == "}":
                d -= 1
                if started and d == 0:
                    return src[i:j + 1]
        return ""

    _uiA = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    _acts_src = _jsfn(_uiA, "phbActs")
    _why_src = _jsfn(_uiA, "phbWhyNot")
    ok(bool(_acts_src) and bool(_why_src),
       "the page decides what a tile offers in one named place")

    # (label, lane, item, must offer, must NOT offer)
    _CASES = (
        ("a PS5 folder app not installed", False,
         {"kind": "homebrew", "shape": "folder", "platform": "PS5", "here": True,
          "installed": False, "title_id": "PPSA50011"}, ["install"], ["get"]),
        ("a PS5 folder app already installed", False,
         {"kind": "homebrew", "shape": "folder", "platform": "PS5", "here": True,
          "installed": True}, ["install"], []),
        ("a PS5 folder app with no way to tell", False,
         {"kind": "homebrew", "shape": "folder", "platform": "PS5", "here": True,
          "installed": None}, ["install"], []),
        ("a PS5 package homebrew", False,
         {"kind": "homebrew", "shape": "pkg", "platform": "PS5", "here": True,
          "installed": False}, ["install"], []),
        ("a PS4 package homebrew", False,
         {"kind": "homebrew", "shape": "pkg", "platform": "PS4", "here": True,
          "installed": False}, ["install"], []),
        ("a stopped payload", False,
         {"kind": "payload", "shape": "elf", "platform": "PS5", "here": True,
          "live": False}, ["run", "send"], []),
        ("a running payload", False,
         {"kind": "payload", "shape": "elf", "platform": "PS5", "here": True,
          "live": True}, ["run", "send"], []),
        ("a payload no PC holds", False,
         {"kind": "payload", "shape": "elf", "platform": "PS5", "here": False,
          "live": None}, ["run"], ["send"]),
        # A TILE OFFERS NOTHING FOR SOMETHING NOBODY HOLDS, and says so in one sentence. At
        # 3.96.0 this was a toast - "Not on this PC" - and fetching lived nowhere near a tile.
        # The Download drawer is where it belongs, and it has its own button and its own row.
        ("something nobody has, with an upstream", False,
         {"kind": "homebrew", "shape": "pkg", "platform": "PS5", "here": False,
          "repo": "someone/thing", "installed": None}, [], ["get", "install"]),
        ("something nobody has and no upstream", False,
         {"kind": "homebrew", "shape": "pkg", "platform": "PS5", "here": False,
          "installed": None}, [], ["install", "get"]),
        ("a folder app aimed at a PS4", False,
         {"kind": "homebrew", "shape": "folder", "platform": "PS4", "here": True,
          "installed": False}, [], ["install"]),
        # THE CONSOLE ALONE HAS TWO VERBS AND NO MORE. /api/payloads/send and /api/payloads/get
        # live on the companion only, and a route on one server and not the other is a silent
        # dead button.
        ("a payload, console answering alone", True,
         {"kind": "payload", "shape": "elf", "platform": "PS5", "here": True,
          "live": False}, ["run"], ["send", "get"]),
        ("a package the console can reach", True,
         {"kind": "homebrew", "shape": "pkg", "platform": "PS5", "here": True,
          "installed": False}, ["install"], ["get"]),
        ("a folder app, console answering alone", True,
         {"kind": "homebrew", "shape": "folder", "platform": "PS5", "here": True,
          "installed": False}, [], ["install"]),
        # `here` ON THAT LANE IS THE CONSOLE'S OWN LIST, and these two are not on it. A helper ELF
        # lives in the owner's folder and is never bundled into the ELF; our own entry is the
        # program serving the page rather than a file in the payload directory. The console answers
        # "This build does not carry that one" for both, so Run must not be offered for either.
        ("a payload the console does not carry", True,
         {"kind": "payload", "shape": "elf", "platform": "PS5", "here": False,
          "live": False, "helper_for": "PPSA97358"}, [], ["run", "send", "get"]),
        ("our own entry on a console with no PC awake", True,
         {"kind": "payload", "shape": "elf", "platform": "PS5", "here": False,
          "live": True, "ours": True}, [], ["run", "send", "get"]),
        # THE OWNER'S COMPLAINT, IN ONE ROW. The console has it installed; no machine the app can
        # see holds the package. There is nothing to press - and the one thing it must not say is
        # that the thing is absent.
        ("an installed homebrew whose bytes are on no machine", False,
         {"kind": "homebrew", "shape": "pkg", "platform": "PS4", "here": False,
          "installed": True}, [], ["install", "get"]),
        # AND THE JAILBREAK LAYER NEVER ACTS ON ONE PRESS. On this lane it has exactly one verb,
        # so without the layer rule a single press started kstuff.
        ("a stopped jailbreak-layer payload, console answering alone", True,
         {"kind": "payload", "shape": "elf", "platform": "PS5", "here": True,
          "live": False, "layer": "jailbreak"}, ["run"], ["send", "get"]),
    )

    # THE REAL phbPress RUNS. It only ever calls toast, phbPaintTile and phbDo, so those three are
    # stubbed and what it DID is recorded. Asserting the text of the condition inside it - which
    # is what this used to do - proves a line was typed, not that a press does anything.
    _press_src = _jsfn(_uiA, "phbPress")
    # THE REAL ONES, not stubs: phbActs now asks phbHelpers which helper ELFs belong to the item
    # it is deciding about, and names them with phbHelperName. Stubbing either would be asserting
    # that this harness is right rather than that the page is.
    _help_src = _jsfn(_uiA, "phbHelpers")
    _hname_src = _jsfn(_uiA, "phbHelperName")
    _js = ("var PHB={onConsole:false,choice:\"\",items:[]};\n"
           "function t(k){return k;}\n"
           "var DID=\"\";\n"
           "function toast(m){DID=\"toast:\"+m;}\n"
           "function phbPaintTile(){DID=\"ask\";}\n"
           "function phbDo(i,b,a){DID=\"do:\"+a;}\n"
           + _help_src + "\n" + _hname_src + "\n"
           + _acts_src + "\n" + _why_src + "\n" + _press_src + "\n"
           + "var C=" + _json4.dumps([[c[0], c[1], c[2]] for c in _CASES]) + ";\n"
           + "var out=[];\n"
           "C.forEach(function(c){ PHB.onConsole=c[1]; PHB.choice=\"\";\n"
           "  var it=c[2]; it._k=\"k\";\n"
           "  var a=phbActs(it);\n"
           "  DID=\"\"; phbPress(it,{});\n"
           "  out.push({label:c[0], keys:a.map(function(x){return x.k;}),\n"
           "            words:a.map(function(x){return x.w;}),\n"
           "            did:DID,\n"
           "            why:a.length?'':phbWhyNot(it)}); });\n"
           "console.log(JSON.stringify(out));\n")
    _f4 = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    _f4.write(_js)
    _f4.close()
    try:
        _r4 = _sub4.run(["node", _f4.name], capture_output=True, text=True, timeout=90)
    except Exception as _e4:
        _r4 = None
        ok(False, "the tile's own decision can be run", "node: %s" % _e4)
    if _r4 is not None:
        ok(_r4.returncode == 0, "the tile's own decision runs clean",
           (_r4.stderr or "")[:400])
        if _r4.returncode == 0:
            _got4 = _json4.loads(_r4.stdout)
            for _c, _g in zip(_CASES, _got4):
                _label, _lane, _item, _must, _mustnot = _c
                _keys = _g["keys"]
                ok(all(m in _keys for m in _must),
                   "%s offers %s" % (_label, " + ".join(_must) or "nothing"),
                   "offered %s" % (_keys or "nothing"))
                ok(not any(m in _keys for m in _mustnot),
                   "...and %s does not offer %s" % (_label, " or ".join(_mustnot) or "-"),
                   "offered %s" % (_keys or "nothing"))
                # A BUTTON WITH NO WORDS ON IT IS NOT AN OFFER.
                ok(all(bool(w) for w in _g["words"]),
                   "...and every button it offers is labelled")
                if not _must:
                    ok(bool(_g["why"]),
                       "...and %s says why there is nothing to press" % _label)
                    ok(_g["did"].startswith("toast:"),
                       "...and pressing %s says so rather than doing nothing" % _label,
                       "it did %r" % _g["did"])
                    # AND THE REASON MUST BE TRUE. "Not on this console" about the program the
                    # reader is looking at is the one sentence here that would be plainly false.
                    if _item.get("live") is True:
                        ok(_g["why"] == "phb_running",
                           "...and a running payload is never reported as absent",
                           "it said %r" % _g["why"])
                    if _item.get("installed") is True:
                        ok(_g["why"] == "phb_installed",
                           "...and an installed homebrew is never reported as absent",
                           "it said %r" % _g["why"])
                else:
                    # 3.96.0'S RULE, WHICH IS THE ONE THE PANEL HAS AGAIN. Written here in the
                    # same shape phbPress has it, so the two cannot drift:
                    #     on = payload ? live===true : installed!==false
                    #     ask when `on`, or when it touches the jailbreak layer
                    # Note what is NOT in it: the number of verbs. It had grown an
                    # `acts.length>1` arm, which meant every tile that picked up a second verb
                    # took two presses - and verbs kept being added, so that was most of them.
                    # A STOPPED payload therefore acts at once, on `run`, exactly as it did at
                    # ca96ce6: phbDo(it, b, "run"). An UNKNOWN state still asks, because reading
                    # null as "not installed" is how one press came to queue a reinstall onto a
                    # console nobody had asked about.
                    _on = (_item.get("live") is True) if _item["kind"] == "payload" \
                        else (_item.get("installed") is not False)
                    _want = ("ask" if (_on or _item.get("layer") == "jailbreak")
                             else ("do:" + _must[0]))
                    ok(_g["did"] == _want,
                       "...and pressing %s %s" % (_label,
                                                  "asks" if _want == "ask" else "does it"),
                       "wanted %r, it did %r" % (_want, _g["did"]))

    # ---- AND THE TILE IT BUILDS FROM THE CONSOLE'S OWN REPLY ------------------------------------
    # On the lane with no PC awake, every field a tile shows is derived from one reply. That
    # derivation was four lines inside a .map() inside a promise chain, and nothing could run it:
    # putting back its old answer - "the ELF carries every payload" - left every check in this
    # file green while the panel offered Run on five files the console cannot start, and said
    # "This build does not carry that one" to each of them.
    _map_src = _jsfn(_uiA, "phbFromConsole")
    _stem_src = _jsfn(_uiA, "phbStem")
    ok(bool(_map_src), "the console-to-tile mapping is a function that can be driven")

    # A reply shaped the way both ELFs send one: `have` is the payload FILES it wrote to its own
    # directory, `kept` the packages it can reach keyed by byte count, `live` what is running.
    _REPLY = {
        "have": [{"n": "ftpsrv.elf", "s": 230640}, {"n": "kstuff.elf", "s": 1737080}],
        "kept": [{"n": "PS5_ITEM00001_v1.14.pkg", "s": 62849024,
                  "p": "/data/pkg-mutant-shop/homebrews/PS5_ITEM00001_v1.14.pkg"}],
        "live": ["kstuff.elf"],
        "apps": ["PPSA50011"],
    }
    _ITEMS = [
        # (label, catalogue entry, here, live, installed)
        ("a bundled payload the console holds",
         {"kind": "payload", "file": "ftpsrv-ps5.elf", "id": "ftpsrv"}, True, False, None),
        ("a bundled payload it is running",
         {"kind": "payload", "file": "kstuff.elf", "id": "kstuff"}, True, True, None),
        ("a helper out of the owner's folder",
         {"kind": "payload", "file": "helper.elf", "id": "xpsemu-helper",
          "helper_for": "PPSA97358"}, False, False, None),
        ("our own entry, which is the running program",
         {"kind": "payload", "file": "PKG-MUTANT-SHOP.elf", "id": "pkg-mutant-shop",
          "ours": True}, False, True, None),
        ("a package the console can reach",
         {"kind": "homebrew", "shape": "pkg", "size": 62849024, "id": "ITEM00001",
          "title_id": "ITEM00001"}, True, None, False),
        ("a package it cannot reach",
         {"kind": "homebrew", "shape": "pkg", "size": 85458944, "id": "PKGI13337",
          "title_id": "PKGI13337"}, False, None, False),
        ("a folder app, whose bytes are on the PC",
         {"kind": "homebrew", "shape": "folder", "size": 44782964, "id": "PPSA50011",
          "title_id": "PPSA50011"}, False, None, True),
        # THE VERSION-DRIFT CASE, which is the one the owner hit. The console holds this package
        # under a path carrying its title id; the size the catalogue recorded is the author's, for
        # an older release, so it is NOT in the kept list. A byte-count match alone could never
        # find it, and on this lane that is the only way a homebrew can be installed at all.
        ("a package the console holds in a different release",
         {"kind": "homebrew", "shape": "pkg", "size": 999999999, "id": "ITEM00001",
          "title_id": "ITEM00001"}, True, None, False),
    ]
    # phbFromConsole leans on phbKeptFor to find the console's copy by title id as well as by
    # byte count, so the harness needs that function too.
    _kept_src = _jsfn(_uiA, "phbKeptFor")
    ok(bool(_kept_src), "the console-copy matcher is a function of its own")
    _js2 = ("function t(k){return k;}\n" + _stem_src + "\n" + _kept_src + "\n" + _map_src + "\n"
            + "var R=" + _json4.dumps(_REPLY) + ";\n"
            + "var IT=" + _json4.dumps([[a, b] for a, b, _c, _d, _e in _ITEMS]) + ";\n"
            + "var live={},kept={},chave={};\n"
              "(R.live||[]).forEach(function(n){ live[phbStem(n)]=1; });\n"
              "(R.kept||[]).forEach(function(e){ if(e&&e.s) kept[e.s]=e.p||e.n; });\n"
              "(R.have||[]).forEach(function(e){ if(e&&e.n) chave[phbStem(e.n)]=1; });\n"
              "var apps=(R.apps||[]).map(function(a){return String(a).toUpperCase();});\n"
              "var out=IT.map(function(p){\n"
              "  var o=phbFromConsole(p[1],live,kept,chave,apps);\n"
              "  return {label:p[0], here:o.here, live:o.live, installed:o.installed,\n"
              "          kept_path:o.kept_path||''}; });\n"
              "console.log(JSON.stringify(out));\n")
    _f5 = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    _f5.write(_js2)
    _f5.close()
    _r5 = _sub4.run(["node", _f5.name], capture_output=True, text=True, timeout=90)
    ok(_r5.returncode == 0, "the mapping runs clean", (_r5.stderr or "")[:400])
    if _r5.returncode == 0:
        for _c5, _g5 in zip(_ITEMS, _json4.loads(_r5.stdout)):
            _lbl, _ent, _where, _lv, _inst = _c5
            ok(_g5["here"] is _where,
               "%s: here is %s" % (_lbl, _where), "it said %r" % _g5["here"])
            ok(_g5["live"] is _lv,
               "...and live is %s" % _lv, "%s: it said %r" % (_lbl, _g5["live"]))
            ok(_g5["installed"] is _inst,
               "...and installed is %s" % _inst, "%s: it said %r" % (_lbl, _g5["installed"]))
        # A PACKAGE IS MATCHED BY BYTE COUNT, so the path the console gave has to come back with
        # it - that path is what the install call on this lane is built from.
        ok(_json4.loads(_r5.stdout)[4]["kept_path"].endswith("PS5_ITEM00001_v1.14.pkg"),
           "...and a reachable package carries the path the console gave for it")
    # AN EMPTY TITLE LIST IS "COULD NOT SAY", NOT "NOTHING IS INSTALLED".
    _js3 = _js2.replace('"apps": ["PPSA50011"]', '"apps": []')
    ok(_js3 != _js2, "the no-titles reply can be built")
    _f6 = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    _f6.write(_js3)
    _f6.close()
    _r6 = _sub4.run(["node", _f6.name], capture_output=True, text=True, timeout=90)
    if _r6.returncode == 0:
        ok(all(_x["installed"] is None for _x in _json4.loads(_r6.stdout)),
           "a console that listed no titles at all is 'could not say', never 'not installed'")

    # ---- WITH NO PC ANSWERING, ART MUST NEVER BE ASKED OF A PC ---------------------------------
    # `thumb_url` and `icon_url` are a COMPANION's absolute addresses, written onto each title and
    # still served by the console long after that companion has gone. On the lane where no PC
    # answered they are requests that hang rather than fail: measured in the console's browser,
    # every cover pending for ever, nothing painted, nothing fell back to initials either.
    #
    # Run, not read: iconUrl() is a pure function of the game and the page's state.
    _icon_src = _jsfn(_uiA, "iconUrl")
    _full_src = _jsfn(_uiA, "iconUrlFull")
    ok(bool(_icon_src) and bool(_full_src), "the page decides a cover's URL in one place")

    _ART = [
        # (label, state, game, must contain, must NOT contain)
        ("on the console, no PC, a build with thumbnails",
         {"onConsole": True, "health": {"thumb_route": True}, "API": "", "webp": None},
         {"title_id": "PPSA01289", "has_icon": True,
          "thumb_url": "http://10.0.0.76:8710/thumb/PPSA01289.webp",
          "icon_url": "http://10.0.0.76:8710/icon/PPSA01289.png"},
         "/thumb/PPSA01289.jpg", "10.0.0.76"),
        ("on the console, no PC, an older build with no thumbnail route",
         {"onConsole": True, "health": {}, "API": "", "webp": None},
         {"title_id": "PPSA01289", "has_icon": True,
          "thumb_url": "http://10.0.0.76:8710/thumb/PPSA01289.webp"},
         "/icon/PPSA01289.png", "10.0.0.76"),
        ("a PC is answering, so its thumbnail is the right one",
         {"onConsole": False, "health": {}, "API": "http://10.0.0.76:8710", "webp": None},
         {"title_id": "PPSA01289", "has_icon": True,
          "thumb_url": "http://10.0.0.76:8710/thumb/PPSA01289.webp"},
         "10.0.0.76:8710/thumb/PPSA01289.webp", ""),
    ]
    _js7 = ("var state={}, PROFILE={}, API=null;\n"
            + _icon_src + "\n"
            + "function safeArtUrl(u){ return !!u && /^https?:\\/\\//.test(String(u)); }\n"
            + "var C=" + _json4.dumps([[a, b, c] for a, b, c, _d, _e in _ART]) + ";\n"
            + "var out=C.map(function(c){\n"
              "  state.onConsole=c[1].onConsole; state.health=c[1].health;\n"
              "  API=c[1].API; PROFILE.webp=c[1].webp;\n"
              "  return iconUrl(c[2]); });\n"
              "console.log(JSON.stringify(out));\n")
    _f7 = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    _f7.write(_js7)
    _f7.close()
    _r7 = _sub4.run(["node", _f7.name], capture_output=True, text=True, timeout=90)
    ok(_r7.returncode == 0, "the cover-URL rule runs clean", (_r7.stderr or "")[:300])
    if _r7.returncode == 0:
        for _c7, _got7 in zip(_ART, _json4.loads(_r7.stdout)):
            _lbl, _st, _g, _must, _never = _c7
            ok(_must in str(_got7), "%s -> %s" % (_lbl, _must), "it asked for %r" % _got7)
            if _never:
                ok(_never not in str(_got7),
                   "...and never a PC that is not answering",
                   "%s asked %r" % (_lbl, _got7))

    # ...and the same for the full-size cover the game panel's banner loads.
    ok("state.onConsole" in _full_src,
       "the full-size cover is not asked of a PC on the console-only lane either",
       "iconUrlFull reads icon_url, which is a companion's address")

    # THE CONSOLES MUST ANSWER THE ROUTE THE PAGE NOW ASKS FOR, and answer it with the original
    # when they have no thumbnail - a 404 would run the cover's error path and leave initials.
    for _src7, _who7 in ((_ps5c, "PS5"), (_ps4c, "PS4")):
        ok('"/thumb/"' in _src7, "the %s serves /thumb/" % _who7)
    # AND THEY MAKE ONE FIRST. The fallback is still there and still matters - a shape
    # shared/pms_thumb.h will not decode, or a title with no art at all - but it is now the THIRD
    # answer, after the cache and after making one on the spot. It used to be the second, and with
    # no PC on the network the cache was always empty, so every card fell back to the full-size
    # 512 px PNG: 25 MB to paint the owner's PS5 library, 7 MB for the PS4's.
    ok("thumb_fill_one" in _ps5c and "thumb_make" in _ps5c,
       "the PS5 makes its own card-sized covers, from the games' own files")
    ok("thumb_fill_one" in _ps4c and "thumb_make" in _ps4c,
       "...and so does the PS4, which with no PC had no small cover at all")
    ok("send_thumb" in _ps5c and "send_icon(fd, tid, req)" in _ps5c,
       "...and the PS5 still falls back to the original rather than 404ing")
    ok("icon_path_for(tid, ip2" in _ps4c,
       "...and so does the PS4")
    ok("thumb_route" in _ps5c and "thumb_route" in _ps4c,
       "...and both say so in /api/health, so an older build is never asked")

    # THE PRESS AND THE ROW READ THE SAME MATRIX. Built separately they drift, which is how a
    # homebrew came to be offered "Send again" and nothing else on a lane where the word for what
    # the button did was "Install".
    _press4 = _jsfn(_uiA, "phbPress")
    ok("phbActs(" in _press4, "the press asks that one place what the item can do")
    _paint4 = _jsfn(_uiA, "phbPaintTile")
    ok("phbActs(" in _paint4, "...and so does the row of buttons it opens")
    ok('it.shape!=="pkg"' not in _press4 and "shape !== \"pkg\"" not in _press4,
       "the press carries no shape refusal of its own")

    # ---- THE REPLY BUFFER MUST FIT ITS PARTS ---------------------------------------------------
    # snprintf truncates in silence. out[2600] held a reply whose parts total about 6.7 KB, so the
    # PS5 answered exactly 2599 bytes of invalid JSON the moment one more field was added - the same
    # shape of failure a 900-byte buffer once caused by cutting a 117-entry title list off at 72.
    # COMMENTS ARE NOT CODE, and this check read one. The note above the buffer explains the bug by
    # naming the old size, so the pattern found "out[2600]" in prose and measured that - which is
    # why shrinking the real buffer back to 2600 left this green. Strip comments first.
    import re as _re2
    # THE WHOLE ROUTE, NOT THE FIRST 9000 BYTES OF IT. The window was a magic number, and the
    # route grew past it: "it was truncated" landed at offset 9024 and this check went red over
    # twenty-four characters, having found nothing wrong. The next route's own `if (!strcmp(path,`
    # is where this one ends, which is the boundary the check actually means - and it keeps the
    # reason the window existed at all, which is that a LATER route declares its own out[340] and
    # would be measured instead.
    # COMMENTS STRIPPED BEFORE THE CUT, not after: a comment 296 bytes into this route quotes
    # `if (!strcmp(path, "/api/payloads"))` while explaining an old bug, so cutting first ended the
    # slice inside the prose and found none of the code.
    _pl = _re2.sub(r"/\*.*?\*/", "", _ps5c, flags=_re2.S)
    _pl = _pl.split('if (!strcmp(path, "/api/payloads"))', 1)[1]
    _cut = _pl.find('if (!strcmp(path, "/api/')
    if _cut > 0:
        _pl = _pl[:_cut]
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
        # EXACTLY THE CATALOGUE, NOT "AT LEAST". `>=` passes for 36 rows as happily as for 29,
        # and that is not hypothetical: a merge that wrote into the memoised shipped catalogue
        # returned 36 rows for this 29-row catalogue, seven programs duplicated, and this
        # assertion was green throughout. A count is either right or it is not.
        ok(len(_i2) == len(items),
           "a folder holding ONE file shows the whole catalogue and nothing twice",
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
    # FOUND, NOT LISTED. This used to name three functions and check each of them, which passed
    # for as long as those were the only three. A fourth size shortcut was then added -
    # install_ours(), placing our own ELF in the payload managers' folders - and it compared the
    # length alone, which is the exact mistake size_can_vouch() exists to prevent, on the exact
    # two files its docstring is about. The gate could not see it: a hardcoded list cannot catch
    # the case nobody thought to add to it.
    #
    # The shortcut has one shape - ask the console how big a file is, compare that to the local
    # length, skip the write if they match - so every line that does it must also ask whether a
    # length can be trusted. Asking console_file_size() for anything else is fine and common:
    # _pldmgr_sidecar uses `is not None` to find out whether a file exists at all, which is a
    # different question and needs no such guard.
    _shortcuts, _unguarded = [], []
    for _ln in _eng2.split("\n"):
        _t = _ln.strip()
        if _t.startswith("#") or "console_file_size(" not in _t or "==" not in _t:
            continue
        _shortcuts.append(_t[:84])
        if "size_can_vouch(" not in _t:
            _unguarded.append(_t[:84])
    ok(len(_shortcuts) >= 3,
       "the gate found the size shortcuts rather than being told them",
       "%d found" % len(_shortcuts))
    ok(not _unguarded,
       "every size shortcut asks whether a length can be trusted",
       "our own ELF keeps its length across releases, so these would skip a real update: "
       + " | ".join(_unguarded))

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
    # A press on an unknown must ASK, not install over the top. CHECKED BY RUNNING IT, further
    # down ("AND THE PAGE MUST OFFER IT") - this used to look for the literal text of the
    # condition, which survives inverting it and dies on reformatting it. What is left here is the
    # part that is genuinely about the words on the tile.
    ok('t("phb_unknown")' in _ui2,
       "a payload nothing listens for has a sentence of its own")
    # The badge must be re-derived from the server, never assumed.
    ok("PHB.ups=PHB.ups.filter" not in _ui2,
       "taking an update no longer empties the update row on trust alone")
    _upone = _ui2.split("function phbUpOne(", 1)[1].split("\nfunction ", 1)[0]
    # phbCheck(FALSE), not true. It must re-ask - assuming the row is gone is how "I installed the
    # update and it shows the update again" went unexplained - but `true` is FORCE, and force on
    # this lane means "ask GitHub again about every tracked project": ten serial reads on the
    # console's single upstream worker, half a minute of "Working..." in the drawer the owner is
    # still looking at. It bought nothing. The only fact that changed is which version this
    # console now holds, and that comes from the note the download lane just wrote.
    ok("phbCheck(false)" in _upone,
       "...it re-asks what is still outstanding, without re-reading every project's release page")
    ok("phbCheck(true)" not in _upone,
       "...and taking one update does not force a cold sweep of the other nine")

    # ---- EVERY payload_engine.X THE SERVER CALLS MUST EXIST --------------------------------------
    # A name that is only ever reached through a module attribute is invisible to the linter and to
    # every test that does not execute that exact line. Writing this gate caught a real one the
    # same hour: _payloads_get called payload_engine.invalidate_live_catalog(), which does not
    # exist and never did - it parsed, it linted clean, and it would have thrown the first time
    # anybody finished a download.
    import re as _re2
    _srv = _io2.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()
    _called = sorted(set(_re2.findall(r"payload_engine\.([A-Za-z_][A-Za-z0-9_]*)", _srv)))
    _absent = [x for x in _called if not hasattr(P, x)]
    ok(not _absent, "every payload_engine.* the server calls exists", ", ".join(_absent))
    ok(len(_called) > 15, "...and the check is actually looking at something", str(len(_called)))

    # ---- AN APP FOLDER IS NOT A FILE TO BE REPLACED ----------------------------------------------
    # For a folder-shaped app the item's recorded path IS the folder, so local_path() answers with a
    # directory. download_asset's backup step then tried to rename the whole app aside, which
    # Windows refuses outright - re-taking XPSemu answered "could not replace the old file" and
    # changed nothing at all. What is downloaded for one of those is an archive BESIDE the folder.
    _pl2 = _io2.open(os.path.join(ROOT, "companion", "payloads.py"), encoding="utf-8").read()
    _da = _pl2.split("def download_asset(", 1)[1].split("\ndef ", 1)[0]
    ok("had_old = os.path.isfile(src)" in _da,
       "download_asset only backs up an old FILE",
       "os.path.exists() is true for the app folder and renaming it fails")
    ok("os.replace(src, bak)" in _da, "...and the backup is still what it was")

    # ---- THE ARCHIVE IS FOUND BY THE NAME IT LANDED AS -------------------------------------------
    # The folder keeps the title id inside it (PPSA97358) and the new archive is identified by the
    # folder it landed in (xpsemu), so looking it up by the updated item's id found nothing: the
    # update reported success and left the archive closed with the old app still in place.
    _oa = _srv.split("def _open_archive(", 1)[1].split("\n    def ", 1)[0]
    ok("fname" in _oa.split("\n", 1)[0], "_open_archive takes the file name")
    ok('c.get("file")' in _oa, "...and matches on it")
    for _lane in ("_payloads_get", "_payloads_update"):
        _body = _srv.split("def %s(" % _lane, 1)[1].split("\n    def ", 1)[0]
        ok("_open_archive(" in _body, "%s opens an archive it fetched" % _lane)
        _args = _body.split("_open_archive(", 1)[1].split("\n\n", 1)[0]
        ok("fname=" in _args, "...and tells it the name it was downloaded as",
           "%s: %s" % (_lane, _args.strip()[:100]))

    # ---- TWO PCs, ONE OF THEM MISSING FILES THE OTHER HAS ----------------------------------------
    # The second PC runs the same exe, so it carries the same BAKED catalogue - built on the first
    # PC, where the emulators are present - while its own folder has none of them. Its live scan
    # therefore offers each one, keyed by the project's name, beside a baked entry for the same
    # program keyed by the title id inside the file. Different keys, so a merge on (id, platform)
    # kept both: the owner's PS5 showed 32 rows where the catalogue has 25, with seven programs
    # listed twice - once "Download", once "Download again".
    #
    # Built here rather than mocked: the baked half is the real catalogue, and the live half is
    # the real one with the emulators taken out, which is exactly what that PC's scan returns.
    _baked = json.load(io.open(CAT, encoding="utf-8"))
    _kept = [i for i in _baked["items"] if i.get("shape") != "folder"]
    _gone = [i for i in _baked["items"] if i.get("shape") == "folder"]
    ok(len(_gone) >= 3, "the catalogue has folder apps to take away", str(len(_gone)))
    _live = {"items": list(_kept) + P.available_items(cur, _kept)}
    _m = P._merged({"items": [dict(i) for i in _baked["items"]]}, _live)["items"]

    _by = {}
    for i in _m:
        _by.setdefault((i.get("platform"), str(i.get("title") or i["id"]).lower()), []).append(i)
    _dup = dict((k, v) for k, v in _by.items() if len(v) > 1)
    ok(not _dup, "a PC missing a file does not list that program twice",
       "; ".join("%s x%d" % (k[1], len(v)) for k, v in sorted(_dup.items())[:4]))

    # ...and the surviving row is the useful one: it still knows the title id, and it says this
    # PC does not hold the bytes so the drawer can offer the download.
    for _g in _gone:
        _row = [i for i in _m
                if str(i.get("title_id") or "") == str(_g.get("title_id") or "")
                and i.get("platform") == _g.get("platform")]
        ok(len(_row) == 1, "%s survives exactly once" % _g.get("title_id"), str(len(_row)))
        if _row:
            ok(_row[0].get("have") is False,
               "...and says this PC does not have it",
               "%s have=%r" % (_g.get("title_id"), _row[0].get("have")))
            ok(not _row[0].get("offer"),
               "...and is a real entry, so its tile still shows",
               "%s offer=%r" % (_g.get("title_id"), _row[0].get("offer")))

    # ---- THE ITEM'S NAME IS COLOURED, AND STAYS COLOURED -----------------------------------------
    # The owner asked for the name to be told apart from the description by colour, and for it to
    # keep that colour on an item they already have: "even if is download again dont gray out the
    # name". It was done, and then a rule written in an earlier pass at this same drawer -
    # `#phbGets .ur.dim .nm{color:var(--muted)}`, from when the row was one line - was left behind
    # further down the stylesheet, where it won on source order. Every downloaded item lost its
    # colour and one row out of eighteen was gold.
    #
    # Source order is the whole mechanism here, so the check reads it: the colour must be set, and
    # nothing after it may set the name's colour again.
    _pg = _io2.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    _css = _pg.split("<style", 1)[1].split("</style>", 1)[0]
    _at = _css.find("#phbGets .ur .nm{")
    ok(_at > 0, "the Get drawer's name has its own rule")
    ok("color:var(--accent)" in _css[_at:_css.find("}", _at)],
       "...and that rule colours it")
    _after = _css[_css.find("}", _at):]
    _later = [ln.strip() for ln in _after.split("\n")
              if ".nm{" in ln and "color:" in ln and "#phbGets" in ln]
    ok(not _later,
       "...and nothing later re-colours it",
       "a rule after it wins on source order: " + "; ".join(_later)[:120])
    ok("#phbGets .ur.dim{opacity:1}" in _css.replace(" ", "").replace("\n", "")
       or "#phbGets .ur.dim{opacity:1}" in _css,
       "...and a row for something already downloaded does not fade as a whole",
       "fading the row fades the name with it, which is the same defect by another route")

    # ---- OUR OWN ELF GOES WHERE THE CONSOLE'S PAYLOAD MANAGER LOOKS ------------------------------
    # The owner asked for our two ELFs to be placed in the folders their payload managers read, so
    # a console can start the app on its own with no PC awake: Payload Manager's
    # /data/pldmgr/payloads/<stem>/<file> on the PS5, GoldHEN's flat /data/payloads on the PS4.
    # ONLY ours - every other payload keeps going exactly where it went before.
    ok(P.GOLDHEN_PAYLOAD_DIR == "/data/payloads",
       "GoldHEN's folder is where GoldHEN reads", P.GOLDHEN_PAYLOAD_DIR)
    ok(P.PLDMGR_DIR == "/data/pldmgr/payloads",
       "Payload Manager's folder is where it resolves /loadpayload against", P.PLDMGR_DIR)

    _ours = [i for i in _scanned if i.get("ours")]
    ok(len(_ours) == 2, "both of our ELFs are in the catalogue", str(len(_ours)))
    _want = {
        "PKG-MUTANT-SHOP.elf": "/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf",
        "PKG-MUTANT-SHOP-PS4.elf": "/data/payloads/PKG-MUTANT-SHOP-PS4.elf",
    }
    for i in _ours:
        ok(P.manager_dest(i) == _want.get(i.get("file")),
           "%s lands where its payload manager looks" % i.get("file"),
           "%r, wanted %r" % (P.manager_dest(i), _want.get(i.get("file"))))
    for i in _scanned:
        if i.get("ours"):
            continue
        ok(P.manager_dest(i) == "",
           "%s is left exactly where it was" % i.get("file"),
           "this feature is for our own ELFs and nothing else")

    # ---- AND EVERY LANE THAT BRINGS A NEW COPY IN REACHES THEM -----------------------------------
    # Traced end to end: the two sends, the Run shortcut that returns early, the download, the
    # update, and the unattended PS4 hand-over. Missing one is the whole defect - it is how the
    # console ends up holding the build before last with nothing on screen to say so.
    _pl3 = _io2.open(os.path.join(ROOT, "companion", "payloads.py"), encoding="utf-8").read()
    # DRIVEN, NOT GREPPED. The first version of this asked whether the function body contained
    # "install_ours(" - and it passed with that call sitting inside `if False:`. A grep cannot
    # tell a reachable call from a dead one. So both send lanes are run here against a bridge
    # that records what it is asked to write, with the loader POST stubbed out, and what is
    # checked is the write.
    class _RecBridge(object):
        def __init__(self):
            self.wrote = []
            self.ip = "10.0.0.1"

        def fs_write(self, path, data, size=None, timeout=None):
            self.wrote.append((path, size))
            return True

        def fs_list(self, path, timeout=None):
            return []                       # nothing on this console yet

    _ourps4 = [i for i in _ours if i.get("platform") == "PS4"]
    _ourps5 = [i for i in _ours if i.get("platform") == "PS5"]
    ok(_ourps4 and _ourps5, "both of ours are available to drive")

    if _ourps4:
        _posted = []
        _real_open = P.urllib.request.urlopen

        class _Resp(object):
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake_open(req, timeout=None):
            _posted.append(getattr(req, "full_url", str(req)))
            return _Resp()

        _rb = _RecBridge()
        P.urllib.request.urlopen = _fake_open
        try:
            _ok4, _msg4 = P.send_ps4("10.0.0.1", {}, _ourps4[0], bridge=_rb)
        finally:
            P.urllib.request.urlopen = _real_open
        ok(_ok4, "the PS4 send still succeeds", str(_msg4)[:80])
        ok(any(":9090" in u for u in _posted),
           "...still hands the bytes to GoldHEN's loader",
           "that POST is what RUNS the payload and must not be replaced by the file write")
        ok(any(p == "/data/payloads/PKG-MUTANT-SHOP-PS4.elf" for p, _z in _rb.wrote),
           "...and leaves a copy where GoldHEN's menu looks",
           "wrote: %r" % ([p for p, _z in _rb.wrote],))

    if _ourps5:
        _rb5 = _RecBridge()
        _rb5.pldmgr = None                  # no Payload Manager: the upload still has to happen
        _ok5, _msg5 = P.send_ps5(_rb5, {}, _ourps5[0])
        ok(any(p == "/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf"
               for p, _z in _rb5.wrote),
           "the PS5 send writes Payload Manager's own copy",
           "wrote: %r" % ([p for p, _z in _rb5.wrote],))
        ok(any(p.endswith("PKG-MUTANT-SHOP.elf.json") for p, _z in _rb5.wrote),
           "...and the sidecar beside it, so a console that never had it lists the folder")

    # A HELPER TAKES THE SAME LANE, AND MUST NOT TAKE OURS. Driven here rather than over the LAN
    # because send_ps5() starts what it uploads - there is no copy-without-running verb for a
    # payload - and rehearsing that would load an unknown payload onto an idle console.
    _helper5 = [i for i in _scanned
                if i.get("helper_for") and i.get("platform") == "PS5"]
    if _helper5:
        _hb5 = _RecBridge()
        _hb5.pldmgr = None
        _hok, _hmsg = P.send_ps5(_hb5, {}, _helper5[0])
        _hdest = P.ps5_payload_dest(_helper5[0])
        ok(any(p == _hdest for p, _z in _hb5.wrote),
           "a helper is uploaded where Payload Manager will run THAT copy",
           "wanted %s, wrote %r" % (_hdest, [p for p, _z in _hb5.wrote]))
        ok(not any("PKG-MUTANT-SHOP" in p for p, _z in _hb5.wrote),
           "...and nothing of ours is written for a payload that is not ours",
           "wrote: %r" % ([p for p, _z in _hb5.wrote],))
        ok(P.manager_dest(_helper5[0]) == "",
           "...which is decided by manager_dest, not by the caller remembering to ask")

    # ...and nothing of ours happens for a payload that is not ours.
    _other4 = [i for i in _scanned
               if i.get("platform") == "PS4" and i["kind"] == "payload" and not i.get("ours")]
    if _other4:
        _posted2 = []
        _real_open2 = P.urllib.request.urlopen

        class _R2(object):
            def read(self):
                return b""

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        def _fake2(req, timeout=None):
            _posted2.append(getattr(req, "full_url", str(req)))
            return _R2()

        _rb2 = _RecBridge()
        P.urllib.request.urlopen = _fake2
        try:
            P.send_ps4("10.0.0.1", {}, _other4[0], bridge=_rb2)
        finally:
            P.urllib.request.urlopen = _real_open2
        ok(not _rb2.wrote,
           "sending somebody else's payload writes no file at all",
           "that lane is unchanged for everything but ours; wrote %r" % (_rb2.wrote,))

    for _lane in ("_payloads_update", "_payloads_get"):
        _body = _srv.split("def %s(" % _lane, 1)[1].split("\n    def ", 1)[0]
        ok("_place_ours(" in _body, "%s places our ELF on the consoles" % _lane,
           "this is the owner's 'downloading it, updating it' case")
    _run = _srv.split('if what in ("send", "run")', 1)[-1].split("\n        if what ==", 1)[0]
    ok("_place_ours(" in _run or "install_ours(" in _run,
       "the PS4 Run shortcut places one too",
       "it returns early when the console's copy is current, so it reached no placement at all")
    _auto = _srv.split("def ps4_start_shop(", 1)[1].split("\ndef ", 1)[0]
    ok("install_ours(" in _auto,
       "the unattended PS4 hand-over leaves a copy behind",
       "a PS4 payload never survives a reboot, so this is how ours usually arrives at all")

    # ---- NONE OF IT MAY FAIL ITS CALLER ----------------------------------------------------------
    # A download works today with every console switched off and has to keep working that way.
    _po = _srv.split("def _place_ours(", 1)[1].split("\n    def ", 1)[0]
    ok("try:" in _po and "except Exception" in _po,
       "placing on a console cannot raise into the lane that called it")
    _io_ours = _pl3.split("def install_ours(", 1)[1].split("\ndef ", 1)[0]
    ok('return True, ""' in _io_ours,
       "...and 'not ours' and 'no console' are both success, not failure")

    # ---- A DRY RUN WRITES NOTHING, ANYWHERE ON THE WAY --------------------------------------------
    # /api/install has honoured dry_run since a test suite installed real games on the owner's
    # console. The extras copy runs BEFORE that route is reached, so for one build "ask what this
    # would do" answered by writing 56 files into /data/PCSX2 - measured. Every console-touching
    # step between the button and _install() has to consult the same flag.
    _pl = _io2.open(os.path.join(ROOT, "companion", "payloads.py"), encoding="utf-8").read()
    _se = _pl.split("def send_extras(", 1)[1].split("\ndef ", 1)[0]
    ok("dry_run" in _se.split("\n", 1)[0], "send_extras takes a dry run")
    _before = _se.split("if dry_run:", 1)[0] if "if dry_run:" in _se else _se
    ok("if dry_run:" in _se, "...and acts on it")
    ok("fs_write" not in _before and "fs_mkdir" not in _before,
       "...before it touches the console",
       "a write above the dry-run return happens whatever the caller asked")
    _act = _srv.split('if what == "install":', 1)[1].split("\n        if what ==", 1)[0]
    ok("send_extras(" in _act, "the install lane places the extras")
    # Up to the line that checks the result - splitting on the first ")" lands inside
    # "log=lambda m: print(m)" and reads an empty argument list, which is a check that passes
    # whatever is written there.
    _call = _act.split("send_extras(", 1)[1].split("if not _exok", 1)[0]
    ok("dry_run" in _call, "...and passes the dry run straight through",
       "the flag exists one layer down and is never consulted: " + _call.strip()[:90])

    # ---- THE CATALOGUE DESCRIBES WHAT IS NOT HERE, AND THE PANEL CAN OFFER IT --------------------
    # The panel's list used to BE the folder listing, so nothing could be offered that had not
    # already been downloaded - the one thing a "get this" feature needs.
    _avail = [i for i in items if i.get("have") is False]
    ok(_avail, "the catalogue carries items that are not in the folder", str(len(_avail)))
    for i in _avail:
        ok(i.get("offer") is True,
           "%s is marked as an invented row" % i.get("id"),
           "the grid hides stubs by this flag, so an unmarked one is drawn as a dead tile")
        ok(bool(i.get("dest_dir")),
           "%s says where it would be downloaded to" % i.get("id"),
           "without dest_dir the download has no destination and refuses")
        ok(bool(i.get("platform")),
           "%s says which console it is for" % i.get("id"))
        ok(bool(i.get("repo")) or bool(i.get("source_only")),
           "%s either has an upstream or says it has none" % i.get("id"))

    # An extension that pick_asset would reject makes an item permanently un-gettable, and the
    # panel reports that as "no file for this console" - true, and impossible to act on.
    for i in items:
        _ext = str(i.get("ext") or "").lower()
        if _ext:
            ok(_ext.startswith("."), "%s's ext is an extension" % i.get("id"), repr(_ext))

    # ---- THE GRID SHOWS WHAT IS HERE; THE DRAWER OFFERS WHAT IS NOT -----------------------------
    _ui3 = _io2.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    _fill = _ui3.split("function phbFill(", 1)[1].split("\nfunction ", 1)[0]
    # ON `offer`, NOT `have`. A second PC whose folder is missing something the first one has
    # gets a REAL entry with have:false - its tile belongs in the grid, saying the bytes are on
    # the other machine - while only the invented stubs must be hidden.
    ok("i.offer===true" in _fill.replace(" ", ""),
       "the tile grid leaves out the invented offer rows",
       "otherwise every offer is also drawn as a dead tile saying 'Not on this PC'")
    # A stub is also recognised by having no path, because an older companion on the LAN does not
    # send `offer` at all - and the console takes its data from whichever PC answers. What must
    # NOT happen is hiding a row that HAS a path: that is a real item this PC merely lacks, and
    # its tile exists to say the bytes are on another machine.
    ok("i.have===false&&!i.path" in _fill.replace(" ", ""),
       "...and recognises a stub from an older companion by its missing path")
    ok("i.have===false)return" not in _fill.replace(" ", ""),
       "...without hiding a real item this PC merely lacks",
       "that is the tile whose whole job is to say the file is on another machine")
    ok("function phbGetsPaint(" in _ui3, "there is a Get drawer")
    ok("phbGetsPaint()" in _ui3.split("function phbPaint(", 1)[-1][:4000] or
       _ui3.count("phbGetsPaint()") >= 2,
       "...and it is repainted with the panel, not only when built")
    ok('id="phbGetdrop"' in _ui3 and 'id="phbUpdrop"' in _ui3,
       "Get and Updates are both there")
    ok(_ui3.find('id="phbGetdrop"') < _ui3.find('id="phbUpdrop"'),
       "...with Get before Updates, as asked")
    ok("function phbDropOpen(" in _ui3,
       "both drawers share one open/close rather than a copied one that can drift")
    ok('api("/api/payloads/get"' in _ui3, "the Download button calls the get route")
    ok('"/api/payloads/get"' in _srv, "...and the server answers it")
    _get = _srv.split("def _payloads_get(", 1)[1].split("\n    def ", 1)[0]
    ok("already" in _get and "force" in _get,
       "a second press on something already here asks before fetching it again")
    ok("extras" in _get, "...and the files an item does not work without come with it")

    # ---------------------------------------------------------------- the console's own lane
    #
    # WITH NO PC, THE CONSOLE FETCHES. Three things said "a PC running this app has to fetch it"
    # because neither ELF had ever called its TLS library. Both now do, so the panel asks the
    # console - and the choosing stays in ONE place, which is what these checks hold.
    _uiL = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    _ps5L = io.open(os.path.join(ROOT, "ps5-app", "onconsole", "server.c"), encoding="utf-8").read()

    for _fn in ("phbUpstream", "phbPickAsset", "phbJobWatch", "phbConsoleGet",
                "phbConsoleCheck", "phbVerSame"):
        ok(("function %s(" % _fn) in _uiL, "the page has %s" % _fn)

    ok('"/api/payloads/upstream"' in _ps5L,
       "the console answers what a project has released")
    ok("GH_BUF_BYTES" in _ps5L and "per_page=3" in _ps5L,
       "...from the list endpoint, not /releases/latest",
       "/releases/latest hides pre-releases, which is what started this")
    ok("upstream_worker" in _ps5L and "pthread_create" in _ps5L,
       "...on a worker thread, because that server answers inline on one accept loop")
    ok("https_download" in _ps5L and 'strncmp(url, "https://", 8)' in _ps5L,
       "the console's existing download lane took on TLS rather than growing a second one")
    ok("host_unreachable_here" in _ps5L,
       "the two hosts this console's TLS cannot reach are named, not discovered as a hex code")
    ok("sha256_file" in _ps5L and "does not match what the project published" in _ps5L,
       "a download is checked against the digest the release stated, before it is put in place")
    ok("signal(SIGPIPE, SIG_IGN)" in _ps5L,
       "a client that hangs up mid-transfer cannot kill the PS5 shop",
       "the PS4 has had this for some time; this build had nothing")

    # THE GATE THAT WOULD HAVE CAUGHT THE FIRST DRAFT. Both matchers, same inputs, same answers.
    _fx = os.path.join(ROOT, "tools", "fixtures", "releases.json")
    ok(os.path.exists(_fx), "the real release documents are checked in to compare against")
    if os.path.exists(_fx):
        _rels = _json4.loads(io.open(_fx, encoding="utf-8").read())
        _pick_src = _jsfn(_uiL, "phbPickAsset")
        ok(bool(_pick_src), "the page's asset choice is one named function")

        # Every catalogue item that names a project, against that project's real release.
        _work = []
        for _it in items:
            _repo = _it.get("repo")
            if not _repo or _repo not in _rels:
                continue
            _work.append({
                "id": _it.get("id"), "platform": _it.get("platform"),
                "kind": _it.get("kind"), "asset": _it.get("asset"), "ext": _it.get("ext"),
                "repo": _repo,
            })
        ok(len(_work) >= 15,
           "there are enough real items to compare (%d)" % len(_work))

        # ...as the page answers it, in node.
        _jsL = ("var REL=" + _json4.dumps(_rels) + ";\n"
                + "var IT=" + _json4.dumps(_work) + ";\n"
                + _pick_src + "\n"
                + "console.log(JSON.stringify(IT.map(function(it){\n"
                  "  var a=phbPickAsset(REL[it.repo],it);\n"
                  "  return a?a.name:null; })));\n")
        _fL = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
        _fL.write(_jsL)
        _fL.close()
        _rL = _sub4.run(["node", _fL.name], capture_output=True, text=True, timeout=120)
        ok(_rL.returncode == 0, "the page's asset choice runs clean",
           (_rL.stderr or "")[:300])

        if _rL.returncode == 0:
            _js_ans = _json4.loads(_rL.stdout)
            # ...and as the companion answers it, in process.
            _agree, _differ = 0, []
            for _w, _got in zip(_work, _js_ans):
                _rel = _rels[_w["repo"]]
                # github_latest() shapes assets as name/size/url/api_url; the fixture carries the
                # first three, which is all pick_asset reads.
                _pyA = P.pick_asset(_rel, _w)
                _want = (_pyA or {}).get("name")
                if _want == _got:
                    _agree += 1
                else:
                    _differ.append("%s/%s: page %r, companion %r"
                                   % (_w["platform"], _w["id"], _got, _want))
            ok(not _differ,
               "the page and the companion choose the same file for all %d items" % len(_work),
               "; ".join(_differ[:4]))

            # AND THE CHECK CAN FAIL. Drop the platform rule the way the first draft did and the
            # comparison above must go red - otherwise it is proving nothing.
            _broken = _pick_src.replace(
                "var named=cands.filter(function(a){", "var named=[].filter(function(a){", 1)
            ok(_broken != _pick_src, "the platform rule is where the check thinks it is")
            _jsB = ("var REL=" + _json4.dumps(_rels) + ";\n"
                    + "var IT=" + _json4.dumps(_work) + ";\n"
                    + _broken.replace("phbPickAsset", "phbPickBroken") + "\n"
                    + "console.log(JSON.stringify(IT.map(function(it){\n"
                      "  var a=phbPickBroken(REL[it.repo],it);\n"
                      "  return a?a.name:null; })));\n")
            _fB = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
            _fB.write(_jsB)
            _fB.close()
            _rB = _sub4.run(["node", _fB.name], capture_output=True, text=True, timeout=120)
            if _rB.returncode == 0:
                _bad = _json4.loads(_rB.stdout)
                _moved = [i for i in range(len(_work)) if _bad[i] != _js_ans[i]]
                ok(bool(_moved),
                   "...and without the platform rule it answers differently, so the check bites",
                   "removing it changed nothing, which means nothing is being tested")

    # WHAT FILE IS THIS? Three implementations - pm_stem on the PS5, p4_stem on the PS4 and
    # phbStem in the page - and they must agree, because every comparison in this panel is made
    # across them: the console reports the name it carries a payload under, the page holds the
    # name the owner sees, and the download lane decides what a new file replaces.
    #
    # They did not agree. Both consoles' walk back over a version suffix stopped on the 'v' and
    # broke, so the branch written to skip it was unreachable and nothing was ever stripped, while
    # the page's regex removed it. Measured on the PS5: after downloading
    # webkit-autoloader-installer_v0.6.1.elf, the _v0.5.0 copy was still sitting beside it.
    _STEMS = [
        "webkit-autoloader-installer_v0.6.1.elf",
        "webkit-autoloader-installer_v0.5.0.elf",
        "webkit-autoloader-installer.elf",
        "pldmgr_v0.5.2.elf",
        "pldmgr.elf",
        "ftpsrv-ps4.elf",
        "ftpsrv-ps5.elf",
        "ftpsrv.elf",
        "nanodns-ps4.elf",
        "nanodns.elf",
        "shadowmountplus.elf",
        "OnionHEN.elf",
        "PKG-MUTANT-SHOP.elf",
        "PKG-MUTANT-SHOP-PS4.elf",
        "pms-installer.elf",
        "kstuff.elf",
        "payload2.elf",
        "ftpsrv-1.16-ng-stable.elf",
    ]
    _stem_js = _jsfn(_uiL, "phbStem")
    ok(bool(_stem_js), "the page's stem rule is one named function")
    _jsS = (_stem_js + "\n"
            + "var N=" + _json4.dumps(_STEMS) + ";\n"
            + "console.log(JSON.stringify(N.map(phbStem)));\n")
    _fS = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    _fS.write(_jsS)
    _fS.close()
    _rS = _sub4.run(["node", _fS.name], capture_output=True, text=True, timeout=90)
    ok(_rS.returncode == 0, "the page's stem rule runs clean", (_rS.stderr or "")[:300])

    # ...and the two C ones, compiled from the files that ship them.
    def _cstem(src_text, fname, names):
        """Compile one console's stem function and run it over `names`."""
        i = src_text.index("static void %s(const char *name, char *out, size_t outsz) {" % fname)
        d, started = 0, False
        body = ""
        for j in range(i, len(src_text)):
            if src_text[j] == "{":
                d += 1
                started = True
            elif src_text[j] == "}":
                d -= 1
                if started and d == 0:
                    body = src_text[i:j + 1]
                    break
        if not body:
            return None
        prog = ('#include <stdio.h>\n#include <string.h>\n\n' + body + '\n'
                'int main(void){\n'
                '  const char *N[] = {' + ",".join('"%s"' % n for n in names) + '};\n'
                '  char o[256];\n'
                '  for (unsigned i=0;i<sizeof(N)/sizeof(N[0]);i++){ %s(N[i],o,sizeof(o));'
                ' printf("%%s\\n", o); }\n'
                '  return 0;\n}\n' % fname)
        cf = _tmp4.NamedTemporaryFile("w", suffix=".c", delete=False, encoding="utf-8")
        cf.write(prog)
        cf.close()
        exe = cf.name[:-2] + ".bin"
        cc = _sub4.run(["gcc", "-O1", "-o", exe, cf.name], capture_output=True, text=True,
                       timeout=120)
        if cc.returncode != 0:
            return ("COMPILE", cc.stderr[:300])
        rr = _sub4.run([exe], capture_output=True, text=True, timeout=60)
        if rr.returncode != 0:
            return ("RUN", rr.stderr[:300])
        return rr.stdout.strip().split("\n")

    _ps4src = io.open(os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c"),
                      encoding="utf-8").read()
    # gcc is not a build dependency of this project - the consoles are cross-compiled - so a
    # machine without one skips the two C halves rather than failing. node is already required.
    try:
        _have_cc = _sub4.run(["gcc", "--version"], capture_output=True,
                             text=True, timeout=30).returncode == 0
    except Exception:
        _have_cc = False
    _a5 = _cstem(_ps5L, "pm_stem", _STEMS) if _have_cc else None
    _a4 = _cstem(_ps4src, "p4_stem", _STEMS) if _have_cc else None
    if not _have_cc:
        ok(True, "the two consoles' stem rules (skipped - no gcc to run them with)")
    else:
        ok(isinstance(_a5, list), "the PS5's stem rule compiles and runs",
           str(_a5)[:300] if not isinstance(_a5, list) else "")
        ok(isinstance(_a4, list), "the PS4's stem rule compiles and runs",
           str(_a4)[:300] if not isinstance(_a4, list) else "")
    if _rS.returncode == 0 and isinstance(_a5, list) and isinstance(_a4, list):
        _ajs = _json4.loads(_rS.stdout)
        _bad = []
        for _k, _nm in enumerate(_STEMS):
            if not (_ajs[_k] == _a5[_k] == _a4[_k]):
                _bad.append("%s -> page %r, PS5 %r, PS4 %r" % (_nm, _ajs[_k], _a5[_k], _a4[_k]))
        ok(not _bad,
           "all three stem rules agree on %d real payload names" % len(_STEMS),
           "; ".join(_bad[:4]))
        # ...and the specific thing that was wrong: a version suffix really is removed.
        ok(_a5[0] == "webkit-autoloader-installer" and _a4[0] == "webkit-autoloader-installer",
           "a _v1.2.3 suffix is stripped on both consoles",
           "PS5 %r / PS4 %r" % (_a5[0], _a4[0]))
        ok(_a5[0] == _a5[1] == _a5[2],
           "two downloads and the bundled copy of one payload share a stem",
           "which is what makes 'replace the previous copy' possible")

    # A CONSOLE THAT CANNOT ANSWER MUST NOT READ AS "NO RELEASES" - and now it does not have to
    # answer at all, because the PAGE can read GitHub itself.
    #
    # Both consoles reply 200 {} to a route they do not have, and the PS4's own GitHub lane fails
    # before TLS is reached at all (send=0x804101e2, which is libnet's RESOLVER_ETIMEDOUT - the
    # name never resolves inside that process). Either way the old answer was null, every project
    # landed in none of the drawer's buckets, and the drawer printed "Everything matches the
    # newest release" about a check that never happened. That is "the ps4 updates are not showing
    # up at all": not "checked and found nothing" but "could not check and said nothing".
    #
    # The page runs in the console's own browser, which has TLS and a resolver that work, and
    # api.github.com sends permissive CORS for public reads - phbSelfCheck has relied on that for
    # this app's own release all along. Three cases, all run:
    #   1 a route that is not there      -> the page reads GitHub and gets the release
    #   2 the console answers an error   -> same, and the page's answer wins
    #   3 GitHub cannot be reached       -> a refusal CARRYING A REASON, never "no releases"
    _REL = ('{"tag_name":"v2.0","html_url":"p","published_at":"2026-10-09T00:00:00Z",'
            '"prerelease":false,"draft":false,'
            '"assets":[{"name":"a.elf","url":"u","size":9,"digest":"sha256:ab"}]}')
    _jsU = ("var CALLS=[],MODE='empty',FETCHES=0;\n"
            "function api(u){ CALLS.push(u);\n"
            "  if(MODE==='error') return Promise.resolve({ok:false,error:'could not reach GitHub'});\n"
            "  return Promise.resolve({}); }\n"
            "function fetch(u,o){ FETCHES++;\n"
            "  if(MODE==='nogh') return Promise.reject(new Error('offline'));\n"
            "  return Promise.resolve({ok:true,status:200,\n"
            "    json:function(){ return Promise.resolve([" + _REL + "]); }}); }\n"
            + _jsfn(_uiL, "phbGhRelease") + "\n"
            + _jsfn(_uiL, "phbUpstream") + "\n"
            "var OUT={};\n"
            "function one(name,mode){ return new Promise(function(done){\n"
            "  MODE=mode; CALLS=[]; FETCHES=0; PHB.gh={};\n"
            "  phbUpstream('owner/'+name, false).then(function(r){\n"
            "    OUT[name]={ok:!!(r&&r.ok), tag:(r&&r.tag)||'', err:(r&&r.error)||'',\n"
            "                isNull:(r===null), api:CALLS.length, gh:FETCHES};\n"
            "    done(); }); }); }\n"
            "one('noroute','empty').then(function(){ return one('conerr','error'); })\n"
            " .then(function(){ return one('nogh','nogh'); })\n"
            " .then(function(){ console.log(JSON.stringify(OUT)); });\n")
    _fU = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    _fU.write("var PHB={gh:{}};\n" + _jsU)
    _fU.close()
    _rU = _sub4.run(["node", _fU.name], capture_output=True, text=True, timeout=90)
    ok(_rU.returncode == 0, "the upstream ask runs clean", (_rU.stderr or "")[:400])
    if _rU.returncode == 0:
        _dU = _json4.loads(_rU.stdout)
        ok(_dU["noroute"]["ok"] and _dU["noroute"]["tag"] == "v2.0",
           "a console with no such route: the PAGE reads the release instead",
           "got %r" % (_dU["noroute"],))
        ok(_dU["noroute"]["gh"] == 1 and _dU["noroute"]["api"] == 1,
           "...asking the console once and GitHub once, not fourteen times",
           "api=%d github=%d" % (_dU["noroute"]["api"], _dU["noroute"]["gh"]))
        ok(_dU["conerr"]["ok"] and _dU["conerr"]["tag"] == "v2.0",
           "a console that answers an error: the page's own read wins",
           "got %r" % (_dU["conerr"],))
        ok((not _dU["nogh"]["ok"]) and _dU["nogh"]["err"] and not _dU["nogh"]["isNull"],
           "neither reachable: a refusal that carries a reason, never 'no releases'",
           "got %r" % (_dU["nogh"],))
        # RATE LIMITING IS THE ONE ERROR NOT WORTH RE-ASKING. It is GitHub refusing this address,
        # and asking again from the same address is how one refusal becomes an hour of them.
        ok("rate-limited" in _jsfn(_uiL, "phbUpstream"),
           "...and a rate-limited console is not re-asked from the same address")
    # phbConsoleCheck is the guard that makes sure the list exists; phbConsoleSweep is the body
    # that does the asking, so the "nobody answered" refusal lives there.
    ok("cannot check for updates" in _jsfn(_uiL, "phbConsoleSweep"),
       "a sweep where nothing answered hands back to the fallback rather than reporting nothing")

    # THE PS4 HAS THE SAME LANE, FROM THE SAME SOURCE. A route on one console only is a silent
    # dead button on the other, which this project has a rule about.
    _ps4L = io.open(os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c"),
                    encoding="utf-8").read()
    _lane = os.path.join(ROOT, "ps4-app", "onconsole", "github_lane.h")
    ok(os.path.exists(_lane), "the PS4 carries the generated GitHub lane")
    ok('#include "github_lane.h"' in _ps4L, "...and includes it")
    for _r4 in ('"/api/payloads/upstream"', '"/api/engine/fetch"', '"/api/download/status"'):
        ok(_r4 in _ps4L, "the PS4 answers %s" % _r4.strip('"'))
        ok(_r4 in _ps5L, "the PS5 answers %s" % _r4.strip('"'))
    for _hook in ("pms_dl_progress", "pms_dl_cancelled", "pms_dl_expected"):
        ok(("static void %s(" % _hook) in _ps4L or ("static int %s(" % _hook) in _ps4L
           or ("static long long %s(" % _hook) in _ps4L,
           "the PS4 implements %s over its own download job" % _hook)
    ok("signal(SIGPIPE, SIG_IGN)" in _ps4L, "the PS4 still ignores SIGPIPE")
    _b4 = io.open(os.path.join(ROOT, "ps4-app", "onconsole", "build-wsl.sh"),
                  encoding="utf-8").read()
    _b5 = io.open(os.path.join(ROOT, "ps5-app", "onconsole", "build-wsl.sh"),
                  encoding="utf-8").read()
    ok("-lSceSsl" in _b4 and "-lSceHttp" in _b4, "the PS4 links the TLS libraries")
    for _bs, _who in ((_b4, "PS4"), (_b5, "PS5")):
        ok("ps4_sync_github_lane.py" in _bs,
           "the %s build refuses a drifted copy of the lane" % _who,
           "a copy nobody checks is a copy that rots")

    # ---------------------------------------------------------------- the Updates drawer, no PC
    #
    # Reported by the owner: "the updates are not being fetched when the companion is off, only
    # works when the companion is up". It was call ORDER. openPHB fired phbLoad() and
    # phbCheck(false) in the same breath, and phbLoad is asynchronous - it asks /api/payloads,
    # fetches the catalogue, and only THEN sets PHB.items. With a companion that did not matter,
    # because /api/payloads/updates answers from the server's own catalogue; with none, the sweep
    # is built FROM PHB.items, so it found no projects, asked nobody, and reported nothing - which
    # on this drawer is indistinguishable from "everything is current".
    #
    # The test exercises the ORDER: the real functions, PHB.items empty, rows required back.
    _UPD_PAY = {
        "ok": True, "on_console": True, "platform": "PS5", "console": "c1",
        "shop_version": "3.97.0", "items": None,
        "hb_dir": "/data/pkg-mutant-shop/homebrews",
        "state": {"live": ["ftpsrv.elf"], "kept": [], "apps": ["PPSA50011"],
                  "have": [{"n": "ftpsrv.elf", "s": 230640, "v": ""},
                           {"n": "webkit-autoloader-installer.elf", "s": 2311424, "v": ""}]},
    }
    _UPD_CAT = {"items": [
        {"id": "ftpsrv", "platform": "PS5", "kind": "payload", "title": "FTP server",
         "repo": "drakmor/ftpsrv", "asset": "ftpsrv", "file": "ftpsrv.elf",
         "version": "1.15-ng-stable", "version_from": "release"},
        {"id": "webkit-autoloader-installer", "platform": "PS5", "kind": "payload",
         "title": "WebKit autoloader installer", "repo": "itsPLK/ps5-webkit-autoloader",
         "asset": "installer", "file": "webkit-autoloader-installer.elf",
         "version": "0.5.2", "version_from": "release"},
        # An INSTALLED FOLDER APP: the console holds no package file for it, which is the case
        # that used to be left out of the comparison altogether.
        {"id": "PPSA50011", "platform": "PS5", "kind": "homebrew", "title": "PS5X360",
         "repo": "BrinooTk/PS5X360", "asset": "PPSA50011", "ext": ".zip",
         "title_id": "PPSA50011", "version": "v0.5.7-fix.1", "version_from": "release"},
    ]}
    _UPD_UP = {
        "drakmor/ftpsrv": {
            "ok": True, "tag": "1.16-ng-stable", "page": "p", "published": "2026-09-20",
            "prerelease": False, "stable_tag": "", "error": "", "age": 1,
            "assets": [{"name": "ftpsrv-ps4.elf", "url": "u1", "size": 166072, "digest": "a" * 64},
                       {"name": "ftpsrv-ps5.elf", "url": "u2", "size": 230640,
                        "digest": "b" * 64}]},
        "itsPLK/ps5-webkit-autoloader": {
            "ok": True, "tag": "v0.6.1", "page": "p", "published": "2026-10-01",
            "prerelease": False, "stable_tag": "", "error": "", "age": 1,
            "assets": [{"name": "webkit-autoloader-installer_v0.6.1.elf", "url": "u3",
                        "size": 3556608, "digest": "c" * 64}]},
        "BrinooTk/PS5X360": {
            "ok": True, "tag": "v0.5.8-fix1", "page": "p", "published": "2026-10-02",
            "prerelease": False, "stable_tag": "", "error": "", "age": 1,
            "assets": [{"name": "PPSA50011.zip", "url": "u4", "size": 99, "digest": "d" * 64}]},
    }
    _UPD_FNS = ["phbStem", "phbKeptFor", "phbFromConsole", "phbSort", "phbLoad", "phbUpstream",
                "phbPickAsset", "phbNewer", "phbVerSame", "phbConsoleCheck", "phbConsoleSweep",
                "phbRowsFor"]

    def _sweep_js(src, pay=None):
        return ("var CALLS=[];\n"
                "var PHB={open:true,items:[],ups:[],platform:\"\",console:\"\",sig:\"\","
                "onConsole:false,consoleVer:\"\",dl:null};\n"
                "var PAY=" + _json4.dumps(pay or _UPD_PAY) + ";\n"
                "var CAT=" + _json4.dumps(_UPD_CAT) + ";\n"
                "var UP=" + _json4.dumps(_UPD_UP) + ";\n"
                "function t(k){ return k; }\n"
                "function toast(){}\n"
                "function phbPaint(){}\n"
                "function phbUpsPaint(){}\n"
                "function esc(x){ return String(x); }\n"
                "function api(u){\n"
                "  CALLS.push(u);\n"
                "  if(u.indexOf('/api/payloads/upstream')===0){\n"
                "    var m=/repo=([^&]+)/.exec(u);\n"
                "    return Promise.resolve(UP[m?decodeURIComponent(m[1]):'']||{});\n"
                "  }\n"
                "  if(u.indexOf('/api/payloads')===0) return Promise.resolve(PAY);\n"
                "  if(u.indexOf('assets/payloads-catalog.json')===0) return Promise.resolve(CAT);\n"
                "  return Promise.resolve({});\n"
                "}\n"
                + "\n".join(_jsfn(src, f) for f in _UPD_FNS) + "\n"
                "phbConsoleCheck(false).then(function(rows){\n"
                "  console.log(JSON.stringify({rows:rows.length,\n"
                "    updates:rows.filter(function(r){return r.newer;}).length,\n"
                "    titles:rows.filter(function(r){return r.newer;})\n"
                "            .map(function(r){return r.title+' -> '+r.asset;}),\n"
                "    haves:rows.map(function(r){return r.title+'='+r.have;}),\n"
                "    asked:CALLS.filter(function(c){return c.indexOf('upstream')>=0;}).length}));\n"
                "}).catch(function(e){ console.log(JSON.stringify({error:String(e)})); });\n")

    def _sweep_run(src, pay=None):
        _f = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
        _f.write(_sweep_js(src, pay))
        _f.close()
        _r = _sub4.run(["node", _f.name], capture_output=True, text=True, timeout=120)
        if _r.returncode != 0:
            return {"error": (_r.stderr or "")[:300]}
        try:
            return _json4.loads(_r.stdout)
        except Exception as _e:
            return {"error": "%s / %s" % (_e, (_r.stdout or "")[:200])}

    _uiL = io.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    _ps5L = io.open(os.path.join(ROOT, "ps5-app", "onconsole", "server.c"),
                    encoding="utf-8").read()
    _ps4A = io.open(os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c"),
                    encoding="utf-8").read()

    _sw = _sweep_run(_uiL)
    ok(not _sw.get("error"), "the update sweep runs from a cold panel", str(_sw.get("error"))[:300])
    ok(_sw.get("rows") == 3, "...and builds a row for every project this console has",
       "it built %r rows" % _sw.get("rows"))
    ok(_sw.get("updates") == 3,
       "...finds all three updates, including the installed folder app",
       "found %r: %r" % (_sw.get("updates"), _sw.get("titles")))
    ok(_sw.get("asked") == 3, "...and actually asked the console about each project",
       "it asked %r times" % _sw.get("asked"))
    ok(any("ftpsrv-ps5.elf" in x for x in (_sw.get("titles") or [])),
       "...and picked the PS5 file out of a release that ships both")

    # AND THE CHECK BITES. Remove the guard - the state the owner reported - and it must go back
    # to reporting nothing.
    _GUARD = ("""  if(!(PHB.items||[]).length)
    return Promise.resolve(phbLoad(true)).then(function(){ return phbConsoleSweep(force); });
  return phbConsoleSweep(force);""")
    ok(_GUARD in _uiL, "the guard is where this check thinks it is")
    if _GUARD in _uiL:
        _sw2 = _sweep_run(_uiL.replace(_GUARD, "  return phbConsoleSweep(force);", 1))
        ok(_sw2.get("rows") == 0 and _sw2.get("asked") == 0,
           "...and without it the sweep reports nothing, which is what was reported",
           "removing the guard changed nothing: %r" % (_sw2,))

    # THE VERSION THE CONSOLE TOOK beats the catalogue's, which is what stops a row offering the
    # same update for ever.
    _pay2 = _json4.loads(_json4.dumps(_UPD_PAY))
    _pay2["state"]["have"][0]["v"] = "1.16-ng-stable"      # ftpsrv, already taken
    _sw3 = _sweep_run(_uiL, _pay2)
    ok(not _sw3.get("error"), "the sweep runs with a version recorded on the console")
    ok("FTP server=1.16-ng-stable" in (_sw3.get("haves") or []),
       "a payload compares against the release the CONSOLE took, not the catalogue's",
       "%r" % (_sw3.get("haves"),))
    ok(_sw3.get("updates") == 2,
       "...so an update already taken stops being offered",
       "still offering %r" % (_sw3.get("titles"),))

    # The console writes that version down, and reports it.
    ok("dl_take_note" in _ps5L and "dl_taken_version" in _ps5L,
       "the console records which release it took, beside the file")
    ok('qparam(rawpath, "tag", rtag' in _ps5L and 'qparam(rawpath, "tag", rtag' in _ps4A,
       "both consoles accept the release tag on a download")
    ok('"v":"%s"' in _ps5L or '\\"v\\":\\"%s\\"' in _ps5L,
       "...and report it in the lists the panel reads")
    ok("&tag=" in _uiL, "the page sends the tag with the download")

    # RUN THE COPY THE OWNER DOWNLOADED - but only one this lane put there.
    ok("if (dtag[0]) {" in _ps5L,
       "Run prefers a downloaded payload only when this lane recorded its release",
       "'not bundled' does not mean 'newer' - the folder holds copies a PC left behind")

    # The rest of the drawer's confirmed defects.
    _ups = _jsfn(_uiL, "phbUpstream")
    # A PENDING REPLY IS NEVER COUNTED AS CHECKED - that is what filled the drawer with dimmed
    # "1.16-ng-stable -> ?" rows under a green "Everything matches the newest release". It no
    # longer answers null either: after the last try the PAGE reads GitHub itself, which is what
    # makes a PS4 - whose own GitHub lane cannot resolve a name at all - able to check.
    ok("if(tries>0)" in _ups and "phbGhRelease(repo,force)" in _ups,
       "a project still pending after the last try is read by the page itself",
       "a pending reply counted as checked, and it carries no version")
    ok("return null;" not in _ups,
       "...so 'nobody answered' is no longer the end of the question")
    for _src, _who in ((_ps5L, "PS5"), (_ps4A, "PS4")):
        ok("if (force && e) e->at = 0;" in _src,
           "a forced update check really refreshes on the %s" % _who,
           "the page's own poll carries no force, so the old answer wins")
    _jw = _jsfn(_uiL, "phbJobWatch")
    ok('j.state==="idle"' in _jw, "a cancelled download ends the wait rather than spinning")
    ok("missed>10" in _jw, "...and so does a job that never appears")
    ok("PHB.dl=jobId" in _jw and "phbDlDone" in _uiL,
       "a running download is visible to the drawer, so it can offer a Cancel")
    ok('id="phbUpStop"' in _uiL and '"/api/download/cancel"' in _uiL,
       "...and that Cancel exists and is wired",
       "phb_cancel was in all fifteen dictionaries and used nowhere")
    _upaint = _jsfn(_uiL, "phbUpsPaint")
    # THE TWO LISTS MUST AGREE, and the test is that they use the same words. The drawer draws a
    # row either as a button or as a link, and "Update all" walks a separately filtered list; while
    # one dropped archives and the other counted them, "Update all" under three visible rows did
    # nothing at all on a PC-less PS5 whose updates were all emulators.
    #
    # AND NEITHER DROPS AN ARCHIVE ANY MORE. Both used to, because neither ELF could open a .zip -
    # which is what turned an available emulator update into a link to somebody's GitHub page.
    # The console installs archives now, so an archive row is a button like every other row and
    # "Update all" takes it. Our own release on a PC is the one case still drawn as a link, and it
    # is the one case both lists still skip: the exe replaces itself down a different road.
    ok("u.archive && PHB.onConsole) return false" not in _upaint,
       "Update all no longer skips an archive on the console",
       "the console can install one now, so there is nothing to skip")
    ok("u.self && !PHB.onConsole) return false" in _upaint,
       "...and still skips our own release on a PC, which replaces itself another way")
    ok("_took++" in _upaint and "phb_upd_took" in _upaint,
       "...carries on past a refusal, and says how many it took",
       "it stopped at the first archive and said nothing")
    ok("!(u.archive && PHB.onConsole)" not in _upaint,
       "Take it anyway is offered for an archive on the console too",
       "it was withheld because every press refused; the press works now")
    # A LINK IS NOW LEFT FOR EXACTLY ONE CASE, and that is worth pinning: an archive row drawn as
    # an <a> to a release page is the owner's original complaint, in markup.
    ok("u.archive && PHB.onConsole && u.url" not in _upaint,
       "an archive update is not drawn as a link to the project's release page",
       "that redirect is the thing the on-console archive lane was built to end")
    ok("Promise.race" in _jsfn(_uiL, "phbSelfCheck"),
       "the page's own release check cannot hang for ever")
    _sweepfn = _jsfn(_uiL, "phbConsoleSweep")
    ok("cannot check for updates" in _sweepfn,
       "a sweep where nothing answered hands back to the fallback")
    ok("no project could be reached" in _uiL,
       "...and so does one where every project errored",
       "a console whose TLS does not work should still get the page's own read")
    ok("phbLoad(true)" in _jsfn(_uiL, "phbConsoleCheck"),
       "a sweep asked before the list exists loads it first",
       "that is the order openPHB used, and it reported nothing")
    ok("i*80" in _sweepfn and "repos.reduce(" not in _sweepfn,
       "every project is asked at once rather than in a chain",
       "the chain left a cold panel at 'working' for over half a minute")

    # And the tiles / Get more drawer.
    ok('want=ext || (it.kind==="homebrew" ? ".pkg" : ".elf")' in _uiL,
       "a package with no `ext` in the catalogue is still installed after downloading",
       "Itemzflow's two entries carry none, so the install step never ran")
    ok("PHB.hbDir ||" in _jsfn(_uiL, "phbConsoleDest"),
       "a downloaded package lands in the folder the console lists")
    ok("o.have = !!(o.here || o.installed===true);" in _jsfn(_uiL, "phbFromConsole"),
       "the build machine's `have` does not reach the console lane")
    ok("o.console_version" in _jsfn(_uiL, "phbFromConsole"),
       "...and the console's own version is carried onto the item")
    _acts = _jsfn(_uiL, "phbActs")
    # NO `get` ON A TILE, which is how 3.96.0 had it - phbActs did not exist there and the row was
    # nine hardcoded lines offering Run and Send again. Fetching belongs to the Download drawer:
    # that drawer is a list of things to get, which is a different question from what this tile can
    # do, and a third verb is what turned one press into a menu.
    ok('k:"get"' not in _acts,
       "a tile does not offer to fetch - that is the Download drawer's job",
       "the owner: \"we only had two buttons in each selection... Run and Send Again\"")
    ok('k:"seed"' not in _acts and "helper_for" not in _acts and "help:" not in _acts,
       "...and it offers no Keep on the console, and no row per helper either")
    # FIVE PUSHES, AND THAT IS THE WHOLE MATRIX: Run and Install on the console lane; Run and
    # Send again for a payload on the PC lane; Send again for a homebrew. Counting them is how
    # this file notices a sixth verb arriving, which is what happened last time.
    ok(_acts.count('out.push(') == 5,
       "...so phbActs pushes exactly five verbs, and they are Run, Install and Send again",
       "%d push(es)" % _acts.count('out.push('))
    # The PC lane keeps the word twice and correctly - a send really does send. What matters is
    # the console branch, which reads the package off the console's own disk.
    _consbranch = _acts[_acts.index("if(PHB.onConsole){"):_acts.index("  if(it.kind===\"payload\"){")]
    ok('t("phb_reinstall")' not in _consbranch,
       "the console branch does not say Send again, because nothing is sent",
       "the console reads the package off its own disk and installs it there")
    ok('phb_ours_downloaded' in _uiL,
       "taking our own release says what is left to do rather than 'Downloaded'")

    # THE PAGE MUST NOT ASK A PC FOR SOMETHING THE CONSOLE CAN DO. The archive row used to carry
    # "a PC running this app unpacks it", which was true until the console carried its own ZIP
    # reader; leaving it there made the drawer contradict the tile - the row said a PC was needed
    # and the console had just unpacked one. It places the app folder at /data/homebrew/<TITLEID>
    # and MERGES into it, which is byte-for-byte what _run_mount does when a PC pushes the same
    # folder (companion/server.py, the `os.path.isdir(local)` branch).
    _gets = _jsfn(_uiL, "phbGetsPaint")
    ok('t("phb_archive_pc")' not in _gets,
       "no row tells the owner a PC is needed to unpack something the console unpacks itself")
    ok('!i.repo || i.source_only || i.ours' in _gets,
       "...and the drawer lists only what pressing it would actually download",
       "a helper with no upstream, or our own app, is not a thing to 'get more' of")
    ok(_gets.count('t("phb_needs_pc")') <= 1,
       "every other row offers a button",
       "the drawer still tells the owner to go and find a PC")
    _upone = _jsfn(_uiL, "phbUpOne")
    ok("phbConsoleGet" in _upone,
       "Update takes the file on the console instead of reporting that it cannot")


    # ---- WHERE A FOLDER APP GOES, AND IT IS NOT THE SYSTEM PARTITION -------------------------
    # MEASURED ON THE PS5 OVER FTP during the restore pass: /mnt/ext1/homebrew held PPSA99203 and
    # PPSA50011 from the PC lane, and /data/homebrew held PPSA99203, PPSA50011 and PPSA99008 from
    # the console's own lane - the SAME title ids in two folders the backup service both scans,
    # with different eboot sizes, because the console installed its updates beside the PC's copies
    # instead of replacing them. That is the owner's "now we have duplicated things inside the
    # folders like homebrew folders", and the cause was one invented default in the page.
    _root = _jsfn(_uiL, "phbConsoleRoot")
    ok('"/data/homebrew"' not in _root and "'/data/homebrew'" not in _root,
       "the page never sends a folder app to /data/homebrew",
       "the 31 GB system partition on a 673 GB console")
    ok("PHB.hbRoot" in _root,
       "...it uses the root the console itself reports")
    ok('PHB.hbRoot=r.hb_root' in _uiL.replace(" ", ""),
       "...which is read out of the /api/payloads reply")
    # The quotes are backslash-escaped in the C source, so the needle carries them too.
    ok('hb_root\\":\\"%s' in _ps5c and "HOMEBREW_DIR" in _ps5c,
       "...and the PS5 puts that root in the reply, from its own constant")
    # AND THE CONSOLE DOES NOT TAKE THE PAGE'S WORD FOR IT. An absolute path was the only test, so
    # a page cached from an older build could still spend 752 MB writing an emulator onto /data.
    ok('strcmp(aroot, HOMEBREW_DIR)' in _ps5c,
       "the console refuses an unpack root that is not its own app drive or a USB drive")

    # ---- AN ARROW BETWEEN TWO EQUAL NUMBERS IS NOT AN UPDATE ---------------------------------
    # Itemzflow holds 01.08 on the PS4 and publishes 1.08: identical. On the PS5 it holds 1.14
    # against an upstream 1.08: we are AHEAD. Both were drawn as "x -> y" rows with a link, in the
    # drawer whose only other output is a count of updates. Both read as an update; one of them
    # offered a downgrade.
    # RUN, NOT READ. This was two substring checks, and both passed while the code was broken on
    # purpose: putting `false &&` in front of phbNewer leaves its text exactly where it was. The
    # decision lives in phbUpsBuckets now - a plain function of its arguments - so the cases
    # measured on the two consoles can be put through the real one.
    _buckets = _jsfn(_uiL, "phbUpsBuckets")
    _same = _jsfn(_uiL, "phbVerSame")
    _newer = _jsfn(_uiL, "phbNewer")
    ok(bool(_buckets) and bool(_same) and bool(_newer),
       "the update drawer's bucket decision is one runnable function")
    # (label, tab, row, bucket) - every row was read off a live console during this pass.
    # "nowhere" means the drawer draws nothing for it: either it is not this tab's console, or
    # there is genuinely nothing to report about it.
    _UPS = [
        ("Itemzflow on the PS4: holds 01.08, publishes 1.08 - the same number", "PS4",
         {"id": "ITEM00001", "platform": "PS4", "have": "01.08", "tag": "1.08",
          "why": "no-files", "newer": False, "error": ""}, "nowhere"),
        ("Itemzflow on the PS5: holds 1.14 against an upstream 1.08 - we are ahead", "PS5",
         {"id": "ITEM00001", "platform": "PS5", "have": "1.14", "tag": "1.08",
          "why": "no-files", "newer": False, "error": ""}, "nowhere"),
        ("RetroArch on the PS5: 01.00 against v1.0.0-beta.1, and there is a file to take", "PS5",
         {"id": "PPSA99169", "platform": "PS5", "have": "01.00", "app_version": "01.00",
          "tag": "v1.0.0-beta.1", "why": "cannot-compare", "newer": False, "error": "",
          "asset": True}, "unsure"),
        ("DolphinPS4: 03.51 -> v04.02, and the lane is sure", "PS4",
         {"id": "DLPH00010", "platform": "PS4", "have": "03.51", "tag": "v04.02",
          "why": "", "newer": True, "error": ""}, "rows"),
        ("a check that failed is a problem, not an update", "PS4",
         {"id": "x", "platform": "PS4", "have": "1", "tag": "2", "newer": False,
          "error": "rate-limited"}, "probs"),
        # THE DRAWER IS PER TAB. A PS5 tab once listed five PS4 payloads and wrote "PS5" beside
        # fourteen rows on a panel whose header already said PS5.
        ("a PS4 update is not drawn on the PS5 tab, however sure the lane is", "PS5",
         {"id": "y", "platform": "PS4", "have": "1", "tag": "2", "newer": True,
          "error": ""}, "not this tab"),
    ]
    _jsU2 = (_same + "\n" + _newer + "\n" + _buckets + "\n"
             + "var C=" + _json4.dumps([[a, b, c] for a, b, c, _d in _UPS]) + ";\n"
             + "var out=C.map(function(c){\n"
               "  var w=phbUpsBuckets([c[2]], c[1]);\n"
               "  if(!w.mine.length)  return 'not this tab';\n"
               "  if(w.rows.length)   return 'rows';\n"
               "  if(w.probs.length)  return 'probs';\n"
               "  if(w.unsure.length) return 'unsure';\n"
               "  return 'nowhere'; });\n"
               "console.log(JSON.stringify(out));\n")
    _fU2 = _tmp4.NamedTemporaryFile("w", suffix=".js", delete=False, encoding="utf-8")
    _fU2.write(_jsU2)
    _fU2.close()
    _rU2 = _sub4.run(["node", _fU2.name], capture_output=True, text=True, timeout=90)
    ok(_rU2.returncode == 0, "...and it runs clean", (_rU2.stderr or "")[:300])
    if _rU2.returncode == 0:
        for (_lbl, _tab, _row, _want), _got in zip(_UPS, _json4.loads(_rU2.stdout)):
            ok(_got == _want, "%s -> %s" % (_lbl, _want), "it landed in %r" % _got)

    # AND THE WATCHER'S THREE BUDGETS ARE DECLARED, not left to become implicit globals. Dropping
    # `odd` from the declaration kept every mention of it in the body, so the old check passed -
    # and `odd` became a global shared by every watcher that ever runs.
    _watchD = _jsfn(_uiL, "phbJobWatch")
    for _c in ("missed", "gone", "odd"):
        ok(("var " + _c + "=0") in _watchD.replace(", ", ", var ").replace("var var ", "var ")
           or (_c + "=0") in _watchD.split("return new Promise")[0],
           "the job watcher declares its %s budget before using it" % _c,
           "an undeclared counter is shared by every watcher at once")

    # ---- A PATCH ON A STICK MUST BE INSTALLABLE ----------------------------------------------
    # The PS4 sorts a USB patch into `updates` and leaves `base` empty, which is correct - but it
    # never emitted update_only, so updOnly() was false for every PS4 row and the footer took the
    # st==="none" road, found no base[0] and toasted "No installable base". The button two blocks
    # below already read "Install update" and was already enabled, so the press refused a label
    # that promised otherwise.
    ok('\\"update_only\\":%s' in _ps4c,
       "the PS4 says when a USB package has no base game of its own")
    ok("g.updates[g.updates.length-1],\"update\",$(\"#installAll\")" in _uiL.replace(" ", "").replace(
           "g.updates[g.updates.length-1],\"update\",$(\"#installAll\")",
           "g.updates[g.updates.length-1],\"update\",$(\"#installAll\")")
       or 'g.updates&&g.updates.length) installItem(g,g.updates[g.updates.length-1],"update"' in _uiL,
       "...and a title with no base installs the update instead of refusing")

    # ---- RUN STARTS WHAT WAS DOWNLOADED, NOT WHAT WAS SHIPPED --------------------------------
    # The PS4's /api/payloads/load asked the bundle FIRST and returned, so the scan for a
    # self-downloaded copy could only be reached for a stem the bundle has no entry for at all.
    # p4_stem strips the version suffix, so an update shares its stem with the shipped build: the
    # owner took an update, pressed Run, and the compiled-in build started.
    _load4 = _ps4c.split('if (!strcmp(path, "/api/payloads/load"))', 1)[1][:8000]
    _dl_at = _load4.find("dl_taken_version")
    # ...AND THE LOOP THAT RUNS THE BUNDLED COPY, not the other mention of the
    # bundle in here: the new block skips names the bundle already covers, so it
    # names PS4_PAYLOAD_BUNDLE too, earlier, and the short needle found that one.
    _bundle_at = _load4.find("const p4pb_entry_t *e = &PS4_PAYLOAD_BUNDLE[i]")
    ok(0 < _dl_at < _bundle_at,
       "the PS4 looks for a downloaded copy BEFORE the bundled one",
       "downloaded check at %d, bundle loop at %d" % (_dl_at, _bundle_at))
    # ...AND ONLY ONE IT DOWNLOADED ITSELF. Preferring any non-bundled file was tried on the PS5
    # and reversed: a second copy a PC left behind can be OLDER (OnionHEN.elf there is 350 KB
    # smaller than the shipped onionhen.elf). The release note is the proof of provenance.
    ok("if (!dtag[0]) continue;" in _load4,
       "...and prefers it only when this lane's own release note is beside it")

    # ---- NO PRESS IS LEFT SAYING "WORKING" FOR EVER -------------------------------------------
    # phbJobWatch gave a job four hours and asked nothing of it in between. Three arms retried
    # without a budget: a reply that was not ok, a transport failure, and a state it does not
    # handle. The owner: "it even gets stuck always".
    _watch = _jsfn(_uiL, "phbJobWatch")
    ok("gone" in _watch and "odd" in _watch,
       "the job watcher budgets transport failures and unknown states, not only time")
    ok(_watch.count("done({ok:false") >= 4,
       "...so every way of not finishing ends the wait and gives the button back",
       "%d terminal arms" % _watch.count("done({ok:false"))
    # SEPARATE COUNTERS, which is not a style point. companion/server.py:4673 carries the warning
    # in its own words: one unlucky connect spent all three strikes in four seconds, on an install
    # that was running perfectly.
    ok(_watch.count("gone=0") >= 1 and "++missed" in _watch,
       "...and a transport failure does not spend the registration budget")

    # ---- A CHAIN THAT THROWS MUST STILL CLEAN UP ---------------------------------------------
    # Each of these disables a control and clears it in a LATER .then, which a throw skips. One
    # throw left PHB.getting true for the rest of the session, and this function refuses every
    # press while it is set - no way back but a reload.
    for _fn, _what in (("phbUpOne", "the Update button comes back if the console lane throws"),
                       ("phbGetOne", "the Download drawer is not left refusing every press")):
        _src = _jsfn(_uiL, _fn)
        _con = _src.split("if(PHB.onConsole){", 1)
        ok(len(_con) > 1 and ".catch(" in _con[1][:1400], _what,
           "%s's console branch has no .catch" % _fn)

    # ---- THE CHECK BUTTON SAYS WHEN IT IS BUSY ------------------------------------------------
    # phbCheck returns at once while a sweep runs - rightly - but the button looked as pressable as
    # ever, and a cold console sweep takes tens of seconds. So the press was thrown away in silence.
    # Read from phbUpsPaint itself: the variable this used to borrow was the old filter block's,
    # and extracting phbUpsBuckets took it away - leaving the check reading something else and
    # reporting a failure about a line that was perfectly fine.
    _upsPaint = _jsfn(_uiL, "phbUpsPaint")
    ok("PHB.checking?' disabled'" in _upsPaint,
       "the Check button is disabled while a check is running")

    # ---- THE TILE GRID CAN REBUILD ITSELF ----------------------------------------------------
    # data-of lives on the element and its children do not have to: anything that empties the box
    # without clearing the attribute leaves a grid that can never rebuild, and the panel then works
    # in every respect except that it has no tiles. The drawer has guarded this for a while.
    _fill = _jsfn(_uiL, "phbFill")
    ok("!box.firstChild" in _fill,
       "an emptied tile grid repaints instead of staying blank for ever")

    # ---- NOTHING SAYS "could not reach GitHub" IN ENGLISH ON A TELEVISION ---------------------
    # The four GitHub markers are codes - phbUpsPaint compares error==="rate-limited" - so they
    # cannot be sentences at the source. They reached the reader through errText, which passes
    # anything already written as a sentence straight through. It is the message the owner quoted.
    ok('"could not reach GitHub":"msg_err_github_unreachable"' in _uiL,
       "the GitHub failure the owner quoted has a key, in all fifteen languages")
    ok('"no releases":"msg_err_no_releases"' in _uiL
       and '"not visible":"msg_err_not_visible"' in _uiL
       and '"rate-limited":"phb_ratelimited"' in _uiL,
       "...and so do the other three answers that lane gives")

    # ---- GOLDHEN'S LOADER IS NEVER PROBED, BY EITHER GATE ------------------------------------
    # :9090 stops listening when something connects and closes without POSTing an ELF, and it is
    # the only road a new PS4 build travels. ready_check dropped it; verify_console kept probing
    # it against the same possibly-PS4 address, so the hazard was still live in one of the two.
    _vc = io.open(os.path.join(ROOT, "tools", "verify_console.py"), encoding="utf-8").read()
    _rc = io.open(os.path.join(ROOT, "tools", "ready_check.py"), encoding="utf-8").read()
    # STRINGS ARE NOT CODE EITHER. Both files now name this port in the sentence that says
    # they do not probe it - "9090 is GoldHEN's loader and is never probed" - so stripping
    # comments alone left the digits behind and this read them as a probe. What must not
    # exist is the port inside a collection handed to port_open.
    import re as _re9
    for _src, _nm in ((_vc, "verify_console"), (_rc, "ready_check")):
        _code = "\n".join(l for l in _src.splitlines() if not l.lstrip().startswith("#"))
        _code = _re9.sub('"[^"\\n]*"', '""', _code)
        _code = _re9.sub("'[^'\\n]*'", "''", _code)
        ok("9090" not in _code, "%s never TCP-probes GoldHEN's loader on :9090" % _nm,
           "a bare connect kills it and only the owner can switch it back on")

    if fails:
        print("test_payloads: FAIL")
        for f in fails:
            print("   %s" % f)
        return 1
    print("test_payloads: OK (%d checks, %d catalogue entries)" % (n, len(items)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

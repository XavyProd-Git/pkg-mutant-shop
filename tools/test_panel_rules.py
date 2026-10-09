# -*- coding: utf-8 -*-
"""Gate: what the Payloads & Homebrews panel means by "Installed", and how long it believes it.

TWO DEFECTS, BOTH REPORTED BY THE OWNER AND BOTH MEASURED BEFORE BEING BELIEVED.

  * The panel said FPKGi was "Installed" on a console it had been DELETED from. The console was
    right, the companion's own /api/installed was right, and the panel was wrong - on one PC. On
    the owner's other PC, which nobody happened to be looking at, the same panel was correct.
    That difference is the whole clue: the probe cache was re-stored with a fresh timestamp on
    every request, including requests that only copied the previous values back out. The panel
    polls every three seconds and the cache expires after four, so looking at the panel kept the
    entry young for ever and the console was asked exactly once - when the panel was opened.

  * "Installed" was also being computed from every title the console's app list MENTIONS. A
    console lists titles it does not have: measured on the owner's PS4, 21 titles of which 5 are
    system entries with no data on disk, and a freshly deleted app keeps its row for a while
    before the console drops it.

Both rules are functions now, so this runs them instead of reading them.
"""
import io
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "companion"))

os.environ.setdefault("PMS_TEST_MODE", "1")
import server as S                                              # noqa: E402

fails = []
n = 0


def ok(cond, what, detail=""):
    global n
    n += 1
    if not cond:
        fails.append("%s%s" % (what, (" - " + detail) if detail else ""))


def main():
    # ---- INSTALLED MEANS INSTALLED -------------------------------------------------------------
    apps = [
        {"title_id": "ITEM00001", "install_status": 0},      # really there
        {"title_id": "LAPY20009", "install_status": 0},      # really there
        {"title_id": "PKGI13337", "install_status": 1},      # the row survives the delete
        {"title_id": "NPXS20114", "install_status": 1},      # a system entry with no data
        {"title_id": "cusa00001", "install_status": 0},      # ids are compared upper-cased
    ]
    got = S.installed_ids(apps)
    ok(got == {"ITEM00001", "LAPY20009", "CUSA00001"},
       "only titles whose data is really in place count as installed", repr(sorted(got or [])))
    ok("PKGI13337" not in (got or set()),
       "a deleted app whose row has not gone yet is NOT installed")
    ok(S.installed_ids(None) is None,
       "no list at all stays unknown, which is not the same as nothing installed")
    ok(S.installed_ids([]) == set(), "an empty list means nothing installed")
    # Absent field: the PS5 path always sets it, but a row that somehow lacks it must not be
    # silently dropped - unknown-but-listed is far likelier to be installed than not.
    ok(S.installed_ids([{"title_id": "AAA"}]) == {"AAA"},
       "a row with no status at all is taken as installed")

    # ---- A CACHED ANSWER AGES FROM WHEN IT WAS MEASURED -----------------------------------------
    # Driven exactly as the route drives it: ask only on a miss, and write back only then.
    cache, asked = {}, []

    def poll(now):
        hit = S.probe_cache_hit(cache, "ps4|10.0.0.86", now)
        if hit is None:
            asked.append(now)
            cache["ps4|10.0.0.86"] = (now, "answer@%g" % now)
            return "answer@%g" % now
        return hit[1]

    poll(0.0)
    ok(asked == [0.0], "the first look asks the console", repr(asked))
    poll(1.0); poll(2.0); poll(3.0)          # the panel polling every three seconds
    ok(asked == [0.0], "...and a look inside the window reuses that answer", repr(asked))
    poll(4.5)
    ok(asked == [0.0, 4.5],
       "...and once the ANSWER is older than the window, the console is asked again", repr(asked))
    # THE BUG, STATED AS A TEST: polling faster than the window must not postpone the next ask.
    cache.clear(); del asked[:]
    t = 0.0
    while t < 30.0:
        poll(t)
        t += 1.0
    ok(len(asked) >= 7,
       "polling every second for 30s still re-reads the console about every 4s, not once",
       "asked %d time(s) at %s" % (len(asked), asked[:9]))

    # ---- AND THE ROUTE REALLY USES THEM ---------------------------------------------------------
    # Extracting a rule helps nothing if the caller kept its own copy.
    import io as _io
    _src = _io.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()
    _fn = _src.split("def _payloads_list(", 1)[1].split("\n    def ", 1)[0]
    ok("installed_ids(" in _fn, "the panel route asks installed_ids() for the word Installed")
    ok("probe_cache_hit(" in _fn, "...and probe_cache_hit() for whether to re-read the console")
    ok("_pc[_ck] = (_now" in _fn and "if _fresh:" in _fn,
       "...and only writes the cache back when it actually measured")

    # ---- THE RESTART WAITS FOR THE PRESSING TO STOP --------------------------------------------
    # "Update all" walks the list one row at a time and our own entry is one of those rows, so
    # restarting the instant that row succeeds cuts the chain off. Measured on this PC: the PS5 row
    # reported success and restarted, and the PS4 row already in flight died with the connection.
    # The restart is armed instead, and every further update request pushes it back.
    #
    # relaunch_self() is replaced here because the real one STARTS A SECOND COPY OF THE APP and
    # exits this process - which a test must never do, and which is also the thing being scheduled.
    import time as _t
    fired = []
    _real_relaunch = S.relaunch_self
    try:
        S.relaunch_self = lambda port, log=None: fired.append(port)
        S._relaunching = False
        S._relaunch_timer = None

        S.schedule_relaunch(8710, delay=0.6)
        _t.sleep(0.25)
        ok(not fired, "arming a restart does not restart anything yet", repr(fired))
        S.defer_relaunch(0.6)                 # another update came in
        _t.sleep(0.45)
        ok(not fired, "...and a second update pushes it back rather than letting it fire",
           repr(fired))
        _t.sleep(0.75)
        ok(fired == [8710], "...and it goes once the panel has been quiet", repr(fired))

        # ONCE, HOWEVER MANY TILES ASK. Our entry appears once per console.
        del fired[:]
        S._relaunching = False
        S._relaunch_timer = None
        S.schedule_relaunch(8710, delay=0.4)
        S.schedule_relaunch(8710, delay=0.4)
        S.schedule_relaunch(8710, delay=0.4)
        _t.sleep(0.9)
        ok(fired == [8710], "three tiles asking still restarts exactly once", repr(fired))

        # Deferring when nothing is armed must not arm one.
        del fired[:]
        S._relaunch_timer = None
        S.defer_relaunch(0.2)
        _t.sleep(0.5)
        ok(not fired, "deferring when no restart is pending does not start one", repr(fired))
    finally:
        S.relaunch_self = _real_relaunch
        try:
            if S._relaunch_timer is not None:
                S._relaunch_timer.cancel()
        except Exception:
            pass
        S._relaunch_timer = None
        S._relaunching = False

    # ---- THE REPLACEMENT IS TOLD WHO IT REPLACED ------------------------------------------------
    # The exe is a PyInstaller bundle: a bootloader parent starts the Python child that runs the
    # app, and the child exiting does not always take the parent with it. Measured after a working
    # self-update - the new build was serving and the OLD build's bootloader was still in the task
    # list holding its own image, which is <exe>.old, so the sweep could not remove it and the NEXT
    # self-update failed trying to rename over it.
    ok(S.REPLACING_FLAG == "--replacing", "the handover has a flag of its own")
    # IT IS GIVEN TIME TO GO BY ITSELF, AND THAT IS THE RULE THAT MATTERS. Killing it the instant
    # the port came free landed in the middle of the bootloader deleting its own ~56 MB extraction
    # folder: two folders from one day were left holding 17 files and 13.6 MB each, identical,
    # because an interrupted rmtree stops in the same place every time. Forcing stays, for the one
    # that genuinely wedges - measured once at nine minutes.
    _killed, _asked = [], []
    _real_run, _real_alive, _real_grace = S.subprocess.run, S._pid_alive, S.HANDOVER_GRACE_SEC

    def _settle(secs=4.0):
        """Wait for the clear-up thread; it must not hold up a build whose job is to serve."""
        _end = _t.time() + secs
        while _t.time() < _end:
            if _killed or (_asked and not _alive_now[0]):
                _t.sleep(0.3)
                return
            _t.sleep(0.05)

    try:
        S.HANDOVER_GRACE_SEC = 0.8
        S.subprocess.run = lambda cmd, **kw: _killed.append(list(cmd))
        _alive_now = [False]
        S._pid_alive = lambda pid: (_asked.append(pid), _alive_now[0])[1]

        # It has already gone: nothing is killed, which is what leaves its cleanup intact.
        S._clear_replaced([S.AFTER_UPDATE_FLAG, "8710", S.REPLACING_FLAG, "4242,4243"])
        _settle()
        ok(not _killed, "a predecessor that closed on its own is not killed", repr(_killed))
        ok(sorted(set(_asked)) == [4242, 4243], "...both of them were asked about", repr(_asked))

        # It wedged: both are ended, so <exe>.old can be replaced.
        del _killed[:], _asked[:]
        _alive_now[0] = True
        S._clear_replaced([S.AFTER_UPDATE_FLAG, "8710", S.REPLACING_FLAG, "4242,4243"])
        _settle()
        _pids = sorted(c[-1] for c in _killed)
        ok(_pids == ["4242", "4243"],
           "one that will not close is ended, after the grace", repr(_killed))
        ok(all("/T" not in c for c in _killed),
           "...by pid alone - a tree kill would include this build, which descends from it")

        del _killed[:], _asked[:]
        # NOTHING NAMED, NOTHING TOUCHED. A normal start must never kill anything.
        S._clear_replaced(["--after-update", "8710"])
        _t.sleep(0.3)
        ok(not _killed and not _asked, "a normal start clears nothing", repr(_killed))
        del _killed[:], _asked[:]
        # AND NEVER OURSELVES, whatever it is told.
        S._clear_replaced([S.REPLACING_FLAG, "%d,0,-1,abc" % os.getpid()])
        _t.sleep(0.3)
        ok(not _killed and not _asked,
           "it refuses to kill its own process, or a nonsense pid", repr(_killed))
    finally:
        S.subprocess.run, S._pid_alive = _real_run, _real_alive
        S.HANDOVER_GRACE_SEC = _real_grace

    # AND THE TWO ENDS ARE WIRED. A clear-up that is never told anything, or never called, is the
    # same as not having one - and neither end can be reached from inside this process: one starts
    # a second copy of the app, the other is the startup path itself.
    import io as _io2
    _ssrc = _io2.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()
    _rl = _ssrc.split("def relaunch_self(", 1)[1].split("\ndef ", 1)[0]
    ok("REPLACING_FLAG" in _rl and "os.getppid()" in _rl,
       "the launch names this process and its bootloader for the replacement to clear")
    _main = _ssrc.split("\ndef main(", 1)[1].split("\ndef ", 1)[0]
    ok("_clear_replaced(" in _main, "...and startup actually clears them")
    ok(_main.index("_clear_replaced(") > _main.index("_wait_for_port_release("),
       "...after waiting for the port, not before")

    # ---- AND NOTHING IS LEFT BESIDE THE APP ----------------------------------------------------
    # <exe>.old is the build that was running and <exe>.new a download that did not finish; both
    # were measured sitting next to the app after a self-update, 38 MB and 25 MB of nothing.
    import payloads as _PE
    import tempfile as _tf
    import shutil as _sh
    _d = _tf.mkdtemp()
    _real_exe = _PE.running_exe
    try:
        _e = os.path.join(_d, "PKG-MUTANT-SHOP.exe")
        for _p in (_e, _e + ".old", _e + ".new"):
            with open(_p, "wb") as f:
                f.write(b"MZ")
        _PE.running_exe = lambda: _e
        _PE.sweep_old_exe()
        ok(not os.path.exists(_e + ".old"), "the previous build is cleared away")
        ok(not os.path.exists(_e + ".new"), "...and so is a download that did not finish")
        ok(os.path.exists(_e), "...and the app itself is untouched")
    finally:
        _PE.running_exe = _real_exe
        _sh.rmtree(_d, ignore_errors=True)

    # ---- A HALF-EXTRACTED BUILD SAYS SO ---------------------------------------------------------
    # A one-file build unpacks itself into %TEMP% before any of this runs, and it can come out
    # incomplete. Measured 2026-10-01: a build started by its own self-update came up with no `web/`
    # in its extraction folder at all, while the exe was byte-for-byte correct. catalog() returns {}
    # for a missing file BY DESIGN, so the app kept answering every request with the entire curated
    # layer gone - every payload lost its repo, the Updates list silently had nothing to offer, and
    # the app could no longer see its own new version. It read as a bug in the update lane.
    import tempfile as _tf2
    import shutil as _sh2
    _real_web, _real_frozen = S.WEB_DIR, getattr(sys, "frozen", False)
    _d6 = _tf2.mkdtemp()
    try:
        sys.frozen = True                      # check_embedded() only speaks for a packaged build
        # A complete unpack says nothing.
        _ok_web = os.path.join(_d6, "web")
        os.makedirs(os.path.join(_ok_web, "assets"))
        for _p in (os.path.join(_ok_web, "index.html"),
                   os.path.join(_ok_web, "assets", "logo.png"),
                   os.path.join(_ok_web, "assets", "payloads-catalog.json")):
            io.open(_p, "w", encoding="utf-8").write("x")
        S.WEB_DIR = _ok_web
        ok(S.check_embedded() == "", "a complete unpack reports nothing", S.check_embedded()[:70])

        # The real failure: the folder is there and `web/` is not.
        S.WEB_DIR = os.path.join(_d6, "gone", "web")
        _msg = S.check_embedded()
        ok(bool(_msg), "a missing web/ is reported")
        ok("open it again" in _msg, "...and it says what to do about it", _msg[:70])
        ok("wrong with the download" in _msg,
           "...and that the download is not the problem, because it is not")

        # One file short counts too - that is how it hides.
        os.remove(os.path.join(_ok_web, "assets", "payloads-catalog.json"))
        S.WEB_DIR = _ok_web
        ok(bool(S.check_embedded()), "one missing file is still an incomplete unpack")

        # Running from source has no extraction to check and must stay silent.
        try:
            del sys.frozen
        except AttributeError:
            pass
        S.WEB_DIR = os.path.join(_d6, "gone", "web")
        ok(S.check_embedded() == "", "running from source reports nothing")
    finally:
        S.WEB_DIR = _real_web
        if _real_frozen:
            sys.frozen = _real_frozen
        else:
            try:
                del sys.frozen
            except AttributeError:
                pass
        _sh2.rmtree(_d6, ignore_errors=True)

    # ---- THE 88.9 MB THAT COULD NOT BE RECLAIMED ------------------------------------------------
    # Every launch of the one-file exe unpacks ~56 MB into %TEMP% and deletes it on the way out. A
    # handover used to interrupt that delete, and what was left had lost the marker files the
    # sweeper proves ownership with - so it could never be claimed by anything again. Measured on
    # this machine: 169 folders, 88.9 MB unclaimable. The fix is to be TOLD the path on the way out
    # instead of inferring it, which also means another program's extraction can never be a
    # candidate. The risk that creates is the opposite one - deleting a folder a live copy is
    # running from - so the delete is gated on a rename, which Windows refuses while a file inside
    # is open, and rmtree(ignore_errors=True) is never pointed at a folder that might be in use.
    import tempfile as _tf3
    import shutil as _sh3
    _d7 = _tf3.mkdtemp()
    _real_note, _real_web2, _real_mp = S.MEI_NOTE, S.WEB_DIR, getattr(sys, "_MEIPASS", None)
    _real_frozen2 = getattr(sys, "frozen", False)
    _real_gettmp = S.tempfile.gettempdir
    _held = None
    try:
        sys.frozen = True
        S.tempfile.gettempdir = lambda: _d7        # keep the real %TEMP% out of a test entirely
        S.MEI_NOTE = os.path.join(_d7, "note.json")

        def _mk(name, with_marker=True):
            p = os.path.join(_d7, name)
            os.makedirs(os.path.join(p, "web", "assets"))
            if with_marker:
                for q in (os.path.join(p, "web", "index.html"),
                          os.path.join(p, "web", "assets", "logo.png")):
                    io.open(q, "w", encoding="utf-8").write("x")
            return p

        gone = _mk("_MEIgone", with_marker=False)   # the half-deleted shape: no marker left
        busy = _mk("_MEIbusy")                      # a live copy is running from this one
        mine = _mk("_MEImine")                      # and this build is running from this one
        vanished = os.path.join(_d7, "_MEIvanished")
        sys._MEIPASS = mine
        _held = io.open(os.path.join(busy, "web", "index.html"), "r", encoding="utf-8")

        S._atomic_write_json(S.MEI_NOTE, [gone, busy, mine, vanished], indent=1)
        S.sweep_mei_leftovers()
        with io.open(S.MEI_NOTE, encoding="utf-8") as _fh:
            _left = json.load(_fh)

        ok(not os.path.isdir(gone), "a folder a killed cleanup left behind is reclaimed")
        ok(gone not in _left, "...and drops off the list")
        ok(os.path.isfile(os.path.join(busy, "web", "index.html")),
           "a folder a live copy is running from is left completely alone")
        ok(busy in _left, "...and stays on the list for a later launch", repr(_left))
        ok(os.path.isfile(os.path.join(mine, "web", "index.html")),
           "it never deletes the folder THIS build is running from")
        ok(mine in _left, "...and keeps its own note for its successor", repr(_left))
        ok(vanished not in _left, "a path that is already gone is forgotten", repr(_left))

        # AND NOTHING IS WRITTEN DOWN BY A BUILD THAT IS STAYING. A note names a folder that is
        # about to be abandoned; naming one at boot would hand a second copy, started from the same
        # directory on another port, permission to delete an extraction in use.
        _rl3 = _ssrc.split("def relaunch_self(", 1)[1].split("\ndef ", 1)[0]
        ok("note_my_extraction()" in _rl3, "the folder is noted as the build hands over")
        _main3 = _ssrc.split("\ndef main(", 1)[1].split("\ndef ", 1)[0]
        ok("note_my_extraction()" not in _main3, "...and never merely because one started")
    finally:
        if _held is not None:
            try:
                _held.close()
            except Exception:
                pass
        S.MEI_NOTE, S.WEB_DIR, S.tempfile.gettempdir = _real_note, _real_web2, _real_gettmp
        if _real_mp is None:
            try:
                del sys._MEIPASS
            except AttributeError:
                pass
        else:
            sys._MEIPASS = _real_mp
        if not _real_frozen2:
            try:
                del sys.frozen
            except AttributeError:
                pass
        _sh3.rmtree(_d7, ignore_errors=True)

    # ---- AND THE PANEL SAYS IT -------------------------------------------------------------------
    # Server-side detection that no device displays is the same as no detection: the symptom of a
    # half-unpacked copy is an EMPTY update list, which looks exactly like having nothing to do.
    _web = _io2.open(os.path.join(ROOT, "web", "index.html"), encoding="utf-8").read()
    # BOTH PLACES IT ARRIVES. The panel builds itself from /api/payloads and refreshes from
    # /api/payloads/updates; reading it in only one of the two is how a banner appears, then
    # vanishes the moment the owner presses Check. Counted, because "is it mentioned anywhere"
    # passed happily with one of the two sites deleted.
    ok(_web.replace(" ", "").count("PHB.degraded=r.degraded") == 2,
       "the panel reads whether the copy answering is complete, on both routes",
       str(_web.replace(" ", "").count("PHB.degraded=r.degraded")))
    ok('"degraded": _degraded,' in _ssrc and _ssrc.count('"degraded": _degraded,') >= 2,
       "...and both routes it builds itself from carry it",
       str(_ssrc.count('"degraded": _degraded,')))
    ok('<div class="ur bad">' in _web, "there is a row for it above the update list")
    ok("phb_app_incomplete" in _web, "...with a translated heading")

    # NO DEAD BUTTON. Both rows can be on screen at once and both offer the same control, so an id
    # made them duplicates - and querySelector binds the first, leaving the lower one inert. This
    # panel has shipped a dead button before and it is not obvious from looking at it.
    ok('id="phbRestart"' not in _web,
       "the restart control is not an id, because there can be two of it")
    ok(_web.count('class="btn primary sm phbRestart"') == 2,
       "...both rows carry it", str(_web.count('class="btn primary sm phbRestart"')))
    ok('querySelectorAll(".phbRestart")' in _web, "...and every one of them is bound")

    # RESTARTING INTO THE SAME VERSION IS NORMALLY REFUSED, and this is the one case where it is
    # the entire remedy - a fresh launch unpacks the files that are missing.
    _rr = _ssrc.split('if path == "/api/app/restart":', 1)[1].split("if path ==", 1)[0]
    ok("not _degraded" in _rr, "a copy that did not unpack properly is allowed to restart itself")

    if fails:
        print("test_panel_rules: FAIL")
        for f in fails:
            print("   %s" % f)
        return 1
    print("test_panel_rules: OK (%d checks)" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

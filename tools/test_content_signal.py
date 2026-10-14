# -*- coding: utf-8 -*-
"""Gate: the companion reacts when a console's content changes, and NOT when it has not.

This is the signal that makes the Updates section respond to an install, an update, a delete or an
add-on made anywhere - our app, the console's own menu, a Store download. It is one string on
/api/health (apps_sig: a stat of app.db and addcont.db) and one reaction (drop the installed-titles
memo, bump library_gen, which is what the page polls).

The failure modes worth gating are all "it fired when it should not have":
  * on the FIRST sighting, which would reload the library once for nothing on every launch
  * when the signature has not moved, which would reload for ever
  * when the payload is too old to report the field, which must behave exactly as before it existed
  * when a console goes quiet and comes back, which is not a content change

No console and no network: the fleet is a stand-in with a counter.

    python tools/test_content_signal.py
"""
import importlib.util
import os
import time
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMPANION = os.path.join(os.path.dirname(HERE), "companion")
sys.path.insert(0, COMPANION)

spec = importlib.util.spec_from_file_location("pms_srv", os.path.join(COMPANION, "server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["pms_srv"] = srv
spec.loader.exec_module(srv)

FAIL = []
RAN = [0]


def ok(cond, what, extra=""):
    RAN[0] += 1
    if not cond:
        FAIL.append(what)
        print("  FAIL  %s%s" % (what, ("   [%s]" % extra) if extra else ""))


class Bridge(object):
    def __init__(self, ip):
        self.ip = ip
        self.dropped = 0

    def invalidate_apps(self):
        self.dropped += 1


class Lib(object):
    def __init__(self):
        self.gen = 7          # not 0, so "it bumped" cannot be confused with "it was initialised"


class Fleet(object):
    def __init__(self, ips):
        self.bridges = {ip: Bridge(ip) for ip in ips}
        self.library = Lib()


def main():
    print("a content change is noticed once, and only when it is a change")
    srv._CONTENT_SIG.clear()
    f = Fleet(["10.0.0.86"])
    con = {"ip": "10.0.0.86", "platform": "ps4", "console_id": "aaaa", "name": "PS4"}
    b = f.bridges["10.0.0.86"]

    # 1. first sighting: remember, do not react
    r = srv.note_content_change(f, {"apps_sig": "100-200-10-20"}, con)
    ok(r is False, "the first sighting does not bump", "returned %r" % r)
    ok(f.library.gen == 7, "library_gen untouched", str(f.library.gen))
    ok(b.dropped == 0, "the installed-titles memo is not dropped")

    # 2. the same signature again: still nothing
    r = srv.note_content_change(f, {"apps_sig": "100-200-10-20"}, con)
    ok(r is False, "an unchanged signature does not bump")
    ok(f.library.gen == 7, "library_gen still untouched", str(f.library.gen))

    # 3. app.db moved: react, exactly once
    r = srv.note_content_change(f, {"apps_sig": "100-999-10-20"}, con)
    ok(r is True, "a changed signature bumps")
    ok(f.library.gen == 8, "library_gen went up by one", str(f.library.gen))
    ok(b.dropped == 1, "and the installed-titles memo was dropped", str(b.dropped))
    r = srv.note_content_change(f, {"apps_sig": "100-999-10-20"}, con)
    ok(r is False and f.library.gen == 8, "and not again for the same state", str(f.library.gen))

    # 4. addcont.db moved on its own: an add-on counts too
    r = srv.note_content_change(f, {"apps_sig": "100-999-10-77"}, con)
    ok(r is True and f.library.gen == 9, "an add-on change counts", str(f.library.gen))

    print("our own install is not announced twice")
    # _finished_installing bumps the gen and stamps _told_ms when one of our tasks lands; forty-five
    # seconds later apps_sig catches up, and without the stamp the page toasts the same install again.
    srv._CONTENT_SIG.clear()
    fx = Fleet(["10.0.0.86"])
    cx = {"ip": "10.0.0.86", "platform": "ps4", "console_id": "aaaa"}
    srv.note_content_change(fx, {"apps_sig": "1-1-1-1"}, cx)       # first sighting
    fx.library.gen += 1                                            # what _finished_installing does
    fx.library._told_ms = time.time()
    r = srv.note_content_change(fx, {"apps_sig": "1-1-1-2"}, cx)
    ok(r is False and fx.library.gen == 8,
       "the echo of our own install does not bump again", str(fx.library.gen))
    # ...but a LATER, separate change does, because two things happened
    fx.library._told_ms = time.time() - 300
    r = srv.note_content_change(fx, {"apps_sig": "1-1-1-3"}, cx)
    ok(r is True and fx.library.gen == 9, "a later change still bumps", str(fx.library.gen))

    print("a console id arrives after the address, and the change in between is not lost")
    # track_consoles learns the id the first time a console reports one, so the memory key moves. The
    # first real change after that used to be swallowed by the first-sighting rule.
    srv._CONTENT_SIG.clear()
    fy = Fleet(["10.0.0.86"])
    no_id = {"ip": "10.0.0.86", "platform": "ps4"}
    with_id = {"ip": "10.0.0.86", "platform": "ps4", "console_id": "bbbb"}
    srv.note_content_change(fy, {"apps_sig": "5-5-5-5"}, no_id)     # first sighting, keyed by address
    r = srv.note_content_change(fy, {"apps_sig": "5-5-5-9"}, with_id)
    ok(r is True and fy.library.gen == 8,
       "the first change after the id is learned is NOT swallowed", str(fy.library.gen))
    ok("10.0.0.86" not in srv._CONTENT_SIG, "and the address entry is not kept for ever")

    print("an older payload, and a console that went quiet")
    # 5. no field at all: behave exactly as before this existed
    before = f.library.gen
    r = srv.note_content_change(f, {"ok": True}, con)
    ok(r is False and f.library.gen == before, "no apps_sig means no opinion", str(f.library.gen))
    r = srv.note_content_change(f, {"apps_sig": ""}, con)
    ok(r is False and f.library.gen == before, "an empty apps_sig means no opinion")

    # 6. a console that stopped answering must not look like a change when it comes back
    r = srv.note_content_change(f, None, con)
    ok(r is False and f.library.gen == before, "no health document at all is not a change")
    r = srv.note_content_change(f, {"apps_sig": "100-999-10-77"}, con)
    ok(r is False and f.library.gen == before,
       "and the remembered signature survived, so waking up is not a change", str(f.library.gen))

    print("two consoles are remembered apart")
    srv._CONTENT_SIG.clear()
    f2 = Fleet(["10.0.0.86", "10.0.0.99"])
    c4 = {"ip": "10.0.0.86", "platform": "ps4", "console_id": "aaaa"}
    c5 = {"ip": "10.0.0.99", "platform": "ps5", "console_id": "bbbb"}
    srv.note_content_change(f2, {"apps_sig": "1-1-1-1"}, c4)
    srv.note_content_change(f2, {"apps_sig": "2-2-2-2"}, c5)
    ok(f2.library.gen == 7, "two first sightings, no bumps", str(f2.library.gen))
    srv.note_content_change(f2, {"apps_sig": "1-1-1-9"}, c4)
    ok(f2.library.gen == 8, "the PS4 changing bumps once", str(f2.library.gen))
    ok(f2.bridges["10.0.0.86"].dropped == 1 and f2.bridges["10.0.0.99"].dropped == 0,
       "and only the PS4's memo is dropped",
       "%d/%d" % (f2.bridges["10.0.0.86"].dropped, f2.bridges["10.0.0.99"].dropped))

    print("an entry with no id at all is followed by address")
    srv._CONTENT_SIG.clear()
    f3 = Fleet(["10.0.0.86"])
    old = {"ip": "10.0.0.86", "platform": "ps4"}        # a config written before ids existed
    srv.note_content_change(f3, {"apps_sig": "1-1-1-1"}, old)
    r = srv.note_content_change(f3, {"apps_sig": "1-1-1-2"}, old)
    ok(r is True and f3.library.gen == 8, "an entry with no console_id still works", str(f3.library.gen))

    print("nothing it does can throw")
    # It runs inside the console tracker's loop; an exception there stops the tracker doing its job.
    # SEEDED FIRST, or this never reaches the fleet at all: a first sighting returns before the
    # bridge or the library is touched, so the check could not fail whatever the code did.
    srv._CONTENT_SIG["1"] = "seeded"
    ok(srv.note_content_change(None, {"apps_sig": "x"}, {"ip": "1"}) is False,
       "no fleet is survivable")
    ok(srv._CONTENT_SIG.get("1") == "x", "...and it got far enough to record the new signature")
    ok(srv.note_content_change(f3, {"apps_sig": "y"}, None) is False, "no console row is survivable")
    ok(srv.note_content_change(f3, "not a dict", {"ip": "1"}) is False, "rubbish is survivable")

    print("\n%d check(s), %d failure(s)" % (RAN[0], len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Which console a request means - the two decisions, both without a console present.

viewer_platform_for()  which console is READING the page (for what the page shows)
_bridge_for()          which console a request is ABOUT (for what the app does)

The shop's UI is ONE file. The PC serves it, the PS5 serves it, the PS4 serves it - and whichever
machine it is read on, the data comes from the PC. So the page cannot tell on its own where it is,
and it has to know: a PS4 must never be offered PS5 games, the header has to name the console the
reader is holding, and the "Game backups" row is a PS5 row that painted a red alarm light on a PS4
that was working perfectly.

The answer is the address the request came from. This is the whole of that decision, tested without
a console, a television or a network.

    python tools/test_viewer_platform.py
"""
import importlib.util
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
COMPANION = os.path.join(os.path.dirname(HERE), "companion")
sys.path.insert(0, COMPANION)

spec = importlib.util.spec_from_file_location("pms_srv", os.path.join(COMPANION, "server.py"))
srv = importlib.util.module_from_spec(spec)
sys.modules["pms_srv"] = srv
try:
    spec.loader.exec_module(srv)
except SystemExit:
    pass

PS5 = {"id": "ps5", "platform": "ps5", "ip": "10.0.0.99"}
PS4 = {"id": "ps4", "platform": "ps4", "ip": "10.0.0.87"}
OLD = {"id": "ps5", "ip": "10.0.0.99"}            # a config written before PS4 support existed

results = []


def case(name, consoles, peer, expect):
    got = srv.viewer_platform_for(consoles, peer)
    ok = got == expect
    results.append((name, ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           "" if ok else "   got %r want %r" % (got, expect)))


case("the PS4 asking is told it is a PS4", [PS5, PS4], "10.0.0.87", "ps4")
case("the PS5 asking is told it is a PS5", [PS5, PS4], "10.0.0.99", "ps5")
# THE DEFAULT HAS TO BE "PC", NOT "PS5". Everything device-specific keys off this string, and a PC
# wrongly answered "ps5" would be shown the console's controller hints and the console's rules
# about what it may install.
case("this PC is not a console", [PS5, PS4], "127.0.0.1", "")
case("another PC on the LAN is not a console", [PS5, PS4], "10.0.0.81", "")
case("no address at all is not a console", [PS5, PS4], "", "")
# A console that does not say what it is, is a PS5 - the same rule reconcile_consoles and the
# health handler follow, because a config from before PS4 support carries no platform field.
case("an entry with no platform reads as a PS5", [OLD], "10.0.0.99", "ps5")
case("an empty fleet answers PC", [], "10.0.0.87", "")
case("an address that matches nothing answers PC", [PS4], "10.0.0.99", "")
# Real fleets get edited by hand; a malformed entry must not take the whole answer down.
case("a junk entry is stepped over, not tripped on", [{"id": "x"}, PS4], "10.0.0.87", "ps4")


# --------------------------------------------------------------------------------------------
# _bridge_for: the console a REQUEST means. Same question, different consequence - this one
# decides which machine gets acted on, and it is the one that could stop the payloads on a
# console nobody asked about.
print("")


class FakeBridge(object):
    def __init__(self, cid, plat, ip):
        self.id, self._plat, self.ip = cid, plat, ip

    def platform_id(self):
        return self._plat


class FakeFleet(object):
    def __init__(self, bridges):
        self._b = {b.id: b for b in bridges}

    def ids(self):
        return list(self._b)

    def bridge(self, cid):
        return self._b.get(cid)


class FakeSrv(object):
    def __init__(self, fleet):
        self.fleet = fleet


class FakeHandler(object):
    def __init__(self, peer="", path="/api/x"):
        self.client_address = (peer, 0)
        self.path = path


B5 = FakeBridge("ps5", "ps5", "10.0.0.99")
B4 = FakeBridge("ps4", "ps4", "10.0.0.87")
SRV = FakeSrv(FakeFleet([B5, B4]))


def bcase(name, handler, body, want, expect):
    got = srv._bridge_for(SRV, handler, body, want)
    gid = got.id if got is not None else None
    ok = gid == expect
    results.append((name, ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           "" if ok else "   got %r want %r" % (gid, expect)))


# 1. NAMED WINS, even against the platform a route would prefer. Quietly doing a PS5-only thing to
#    the other console because the named one is "wrong" is how you act on a machine nobody asked
#    about; the named console's own refusal is the honest answer.
bcase("console in the body wins", FakeHandler(), {"console": "ps4"}, None, "ps4")
bcase("console in the query wins", FakeHandler(path="/api/x?console=ps4"), None, None, "ps4")
bcase("a named PS4 beats want=ps5", FakeHandler(), {"console": "ps4"}, "ps5", "ps4")
# 2. THE REQUESTER. The page is served BY the console it is read on, so its address is the answer.
bcase("the asking console is the answer", FakeHandler("10.0.0.87"), None, None, "ps4")
bcase("a named console still beats the asker",
      FakeHandler("10.0.0.87"), {"console": "ps5"}, None, "ps5")
# 3. A REQUIRED PLATFORM, for the few things only one kind of console has.
bcase("want=ps5 from a PC finds the PS5", FakeHandler("127.0.0.1"), None, "ps5", "ps5")
bcase("want=ps4 from a PC finds the PS4", FakeHandler("127.0.0.1"), None, "ps4", "ps4")
# 4. "all" IS NOT A CONSOLE - it is what the picker sends for "every console", and the reads that
#    use this want one. It must fall through, not fail.
bcase("console=all falls through to the ordinary order",
      FakeHandler("10.0.0.87"), {"console": "all"}, None, "ps4")
bcase("an unknown name falls through rather than failing",
      FakeHandler("10.0.0.87"), {"console": "nope"}, None, "ps4")
# 5. Nothing to choose from.
_empty = FakeSrv(FakeFleet([]))
results.append(("an empty fleet answers None",
                srv._bridge_for(_empty, FakeHandler(), None, None) is None))
print("  [%s] an empty fleet answers None" % ("PASS" if results[-1][1] else "FAIL"))
_only5 = FakeSrv(FakeFleet([B5]))
results.append(("want=ps4 with no PS4 answers None, never the PS5",
                srv._bridge_for(_only5, FakeHandler(), None, "ps4") is None))
print("  [%s] want=ps4 with no PS4 answers None, never the PS5" % ("PASS" if results[-1][1] else "FAIL"))

bad = [n for n, ok in results if not ok]
print("\n%d checks, %d failed" % (len(results), len(bad)))
sys.exit(1 if bad else 0)

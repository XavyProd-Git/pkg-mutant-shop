# -*- coding: utf-8 -*-
"""Regression gate for the storage tiles on the main page.  Run: python tools/test_storage_tiles.py

THIS BUG CAME BACK ONCE. The duplicate M.2 tile was fixed in 3.51.0 by folding app.db's location
buckets into the real drives, and it returned in 3.53.0 - because the fold is conditional on the
console having reported that drive, and with the PS5 switched off it had reported nothing. The
duplicate was only ever visible OFFLINE, which is why testing it with the console on kept passing.

The synthetic library is the shape the 3.53.0 report showed: 34 backups bucketed onto ext1 by their
container paths, 6 packages that only app.db knows about carrying location "2" (extended), 51
carrying location "0" (internal), and 19 backups on usb0. Those four counts are the ones that
appeared on the four hollow tiles.

The invariant at the end is the real guard: a console drive tile ALWAYS has real free/total behind
it. Anything that reintroduces a tile reading "space unknown" fails here.
"""
import sys, os, types
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "companion"))
import server

APPS = (
    [{"platform": "PS5", "backup_path": "/mnt/ext1/homebrew/g%d.ffpfsc" % i, "size": 40 * 10**9}
     for i in range(34)] +
    [{"platform": "PS4", "location": "2", "size": 7 * 10**9} for _ in range(6)] +
    [{"platform": "PS4", "location": "0", "size": 15 * 10**9} for _ in range(51)] +
    [{"platform": "PS5", "backup_path": "/mnt/usb0/homebrew/u%d.ffpfsc" % i, "size": 90 * 10**9}
     for i in range(19)]
)
LIVE_DEVS = [
    {"id": "internal", "label": "Internal SSD", "free": 120 * 10**9, "total": 800 * 10**9, "detected": True},
    {"id": "ext1", "label": "Extended Storage", "free": 300 * 10**9, "total": 2000 * 10**9, "detected": True},
    {"id": "usb0", "label": "USB0", "free": 100 * 10**9, "total": 2000 * 10**9, "detected": True},
    {"id": "ext0", "label": "Extended (ext0)", "detected": False},
]

srv = types.SimpleNamespace(cfg={"companion": {"port": 8710, "host": "0.0.0.0"}, "library": {}}, peers=None)
server._library_counts = lambda s: {"total": 0, "bytes": 0}     # isolate the console half
server._library_capacity = lambda s: {}


def show(tag, out):
    print("\n--- %s   reachable=%s ---" % (tag, out["reachable"]))
    for d in out["drives"]:
        cap = ("%.0f free of %.0f GB" % (d["free"] / 1e9, d["total"] / 1e9)) \
            if (d.get("free") is not None and d.get("total")) else "space unknown"
        print("   %-10s %-20s %-22s %d games" % (d["id"], d["label"], cap, d["count"]))
    return out["drives"]


print("=" * 78)
print("THE SCREENSHOT'S CALL, unchanged: no console_online argument, no live devices.")
print("This is the exact call that drew EXT1 / Internal SSD / Extended Storage / USB0, every one")
print("of them 'space unknown' and the M.2 twice. Both fixes independently close it, so the old")
print("behaviour is not reachable even through the legacy signature.")
d = show("PS5 OFF, legacy call", server.build_storage(srv, APPS, None))
ids = [x["id"] for x in d]
assert d == [], "the legacy call must no longer produce hollow tiles, got %s" % ids
print("   => no tiles. The duplicate cannot be reproduced any more. PASS")

print("\n" + "=" * 78)
print("AFTER THE FIX")
d = show("PS5 OFF (console_online=False)", server.build_storage(srv, APPS, None, console_online=False))
assert d == [], "console drives must not be listed while the PS5 is off, got %s" % d
print("   => no console tiles at all. PASS")

d = show("PS5 ON (console_online=True, live devices)", server.build_storage(srv, APPS, LIVE_DEVS, console_online=True))
ids = [x["id"] for x in d]
assert "loc:2" not in ids and "loc:0" not in ids, "app.db buckets must fold into real drives: %s" % ids
assert sorted(ids) == ["ext1", "internal", "usb0"], ids
assert all(x["free"] is not None for x in d), "every tile must carry real capacity"
byid = dict((x["id"], x) for x in d)
assert byid["ext1"]["count"] == 40, "ext1 must hold 34 backups + 6 packages, got %d" % byid["ext1"]["count"]
assert byid["internal"]["count"] == 51, byid["internal"]["count"]
print("   => 3 tiles, no duplicate, real capacity, ext1 = 34 backups + 6 packages = 40. PASS")

print("\n" + "=" * 78)
print("ALL ASSERTIONS PASSED")

print("\n" + "=" * 78)
print("DRIVE PULLED WHILE THE CONSOLE STAYS AWAKE")

# usb0 unplugged: the console is up and answering, but usb0 is gone. app.db still lists its titles.
DEVS_NO_USB = [d for d in LIVE_DEVS if d["id"] != "usb0"] + [
    {"id": "usb0", "label": "USB0", "detected": False}]
d = show("usb0 pulled, console awake", server.build_storage(srv, APPS, DEVS_NO_USB, console_online=True))
ids = [x["id"] for x in d]
assert "usb0" not in ids, "a pulled stick must not keep its tile: %s" % ids
assert sorted(ids) == ["ext1", "internal"], ids
assert all(x["free"] is not None for x in d)
print("   => usb0 tile gone, the rest keep real capacity. PASS")

# M.2 removed: no extended drive at all, but rows still carry location "2" -> loc:2 cannot fold.
DEVS_NO_EXT = [{"id": "internal", "label": "Internal SSD", "free": 120 * 10**9,
                "total": 800 * 10**9, "detected": True},
               {"id": "usb0", "label": "USB0", "free": 100 * 10**9,
                "total": 2000 * 10**9, "detected": True}]
d = show("M.2 removed, stale location-2 rows remain", server.build_storage(srv, APPS, DEVS_NO_EXT, console_online=True))
ids = [x["id"] for x in d]
assert "loc:2" not in ids, "an unfoldable app.db bucket must not become a tile: %s" % ids
assert "ext1" not in ids, "a removed M.2 must not keep its tile: %s" % ids
assert sorted(ids) == ["internal", "usb0"], ids
print("   => no orphan 'Extended Storage' tile, no ext1 tile. PASS")

# The invariant that matters: a console tile ALWAYS has real capacity behind it.
for devs in (LIVE_DEVS, DEVS_NO_USB, DEVS_NO_EXT):
    for t in server.build_storage(srv, APPS, devs, console_online=True)["drives"]:
        assert t["free"] is not None and t["total"], "hollow tile: %s" % t
print("\n   INVARIANT HOLDS: no console tile can ever read 'space unknown' again.")

print("\n" + "=" * 78)
print("ALL ASSERTIONS PASSED (extended)")

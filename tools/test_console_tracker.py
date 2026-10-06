# -*- coding: utf-8 -*-
"""track_consoles: following a console that moved, and refusing to guess when it cannot be sure.

THE DEFECT THIS EXISTS FOR WAS REAL AND MEASURED. The PS4 restarted, took a new DHCP lease, came
back at 10.0.0.86, and config.json still said 10.0.0.87. Discovery found it - `confirmed=True
platform=ps4` - and nothing compared that against what was saved, so a healthy console was shown as
offline indefinitely and every PS4 route aimed at an empty address.

The fix is allowed to REWRITE A CONSOLE'S ADDRESS, which is the one thing the other adoption passes
deliberately never do. That makes its refusals as important as its successes: an entry moved onto
the wrong console points installs, deletes and cheat syncs at a machine the owner never named. So
every "does nothing" case below is a guard, not an omission.

No console and no network: console_probe and discover_ps5 are replaced with a fake network.

    python tools/test_console_tracker.py
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

results = []


def fake_net(hosts):
    """hosts: {ip: (platform, console_id)} - everything else on the network is silent."""
    def probe(ip, timeout=1.5):
        if ip not in hosts:
            return None
        plat, cid = hosts[ip]
        d = {"ok": True, "on_console": True, "platform": plat, "lan_ip": ip}
        if cid:
            d["console_id"] = cid
        return d

    def discover(cfg):
        return [{"ip": ip, "ports": [8710], "confirmed": True, "platform": hosts[ip][0]}
                for ip in sorted(hosts)]

    srv.console_probe = probe
    srv.discover_ps5 = discover


def case(name, consoles, hosts, expect_ips, expect_changed=None):
    fake_net(hosts)
    cfg = {"consoles": [dict(c) for c in consoles], "ftp": {"port": 2121}}
    srv.save_config = lambda *a, **k: None          # never touch a real config from a test
    changed = srv.track_consoles(cfg, None, log=lambda m: None)
    got = [(c.get("id"), c.get("ip")) for c in cfg["consoles"]]
    ok = got == expect_ips
    if expect_changed is not None:
        ok = ok and (len(changed) == expect_changed)
    results.append((name, ok))
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))
    if not ok:
        print("        got     %s  (%d change(s))" % (got, len(changed)))
        print("        want    %s%s" % (expect_ips,
                                        "" if expect_changed is None
                                        else "  (%d change(s))" % expect_changed))
        for m in changed:
            print("        said:   %s" % m)


print("track_consoles")

# ---- the measured case: a PS4 moved, and it reports the id we already recorded.
case("a console that moved is followed by its id",
     [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.99", "console_id": "aaaa000000000001"},
      {"id": "ps4", "platform": "ps4", "ip": "10.0.0.87", "console_id": "bbbb000000000002"}],
     {"10.0.0.99": ("ps5", "aaaa000000000001"),
      "10.0.0.86": ("ps4", "bbbb000000000002")},
     [("ps5", "10.0.0.99"), ("ps4", "10.0.0.86")], 1)

# ---- the same, for a config written before identity existed. One candidate, so it is safe.
case("with no id, the only unclaimed PS4 is taken",
     [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.99"},
      {"id": "ps4", "platform": "ps4", "ip": "10.0.0.87"}],
     {"10.0.0.99": ("ps5", None), "10.0.0.86": ("ps4", None)},
     [("ps5", "10.0.0.99"), ("ps4", "10.0.0.86")])

# ---- TWO candidates and no id: unknowable, so it must do nothing.
case("two possible PS4s and no id - address left alone",
     [{"id": "ps4", "platform": "ps4", "ip": "10.0.0.87"}],
     {"10.0.0.86": ("ps4", None), "10.0.0.88": ("ps4", None)},
     [("ps4", "10.0.0.87")], 0)

# ---- a PS5 must never land on a PS4 entry, whatever else is on the network. The PS5 IS adopted
# here - it is a platform with no entry - and that is right; what matters is that the silent PS4
# entry keeps its own address instead of being pointed at a console of the wrong kind.
case("a PS5 is never used to fix a PS4 entry (it is adopted separately)",
     [{"id": "ps4", "platform": "ps4", "ip": "10.0.0.87"}],
     {"10.0.0.99": ("ps5", "aaaa000000000001")},
     [("ps4", "10.0.0.87"), ("ps5", "10.0.0.99")], 1)

# ---- the candidate's id belongs to a DIFFERENT entry: that is the other console, not this one.
case("a console another entry already owns is not stolen",
     [{"id": "ps4", "platform": "ps4", "ip": "10.0.0.87", "console_id": "bbbb000000000002"},
      {"id": "ps4-1", "platform": "ps4", "ip": "10.0.0.90", "console_id": "cccc000000000003"}],
     {"10.0.0.90": ("ps4", "cccc000000000003")},
     [("ps4", "10.0.0.87"), ("ps4-1", "10.0.0.90")], 0)

# ---- everything where it should be: no scan, no change, no noise.
case("nothing changes when every console answers",
     [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.99", "console_id": "aaaa000000000001"}],
     {"10.0.0.99": ("ps5", "aaaa000000000001")},
     [("ps5", "10.0.0.99")], 0)

# ---- an id is learned the first time a console reports one, so it can be followed NEXT time.
# ...and it has to reach the DISK. Setting the field and never saving meant every restart
# re-learned it, so the id only ever existed in memory - which defeats the one thing an id is for.
fake_net({"10.0.0.99": ("ps5", "aaaa000000000001")})
_cfg = {"consoles": [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.99"}], "ftp": {"port": 2121}}
_saved = []
srv.save_config = lambda *a, **k: _saved.append(1)
srv.track_consoles(_cfg, None, log=lambda m: None)
_ok = _cfg["consoles"][0].get("console_id") == "aaaa000000000001"
results.append(("an id is learned from a console that is where we expect it", _ok))
print("  [%s] an id is learned from a console that is where we expect it"
      % ("PASS" if _ok else "FAIL"))
_ok2 = len(_saved) == 1
results.append(("...and learning it writes the config", _ok2))
print("  [%s] ...and learning it writes the config%s"
      % ("PASS" if _ok2 else "FAIL", "" if _ok2 else "  (save_config called %d time(s))" % len(_saved)))

# ---- THE RULE THAT REPLACED "an identifiable console is never assumed to be an unidentifiable
# entry", and the reason it had to change.
#
# That case asserted the opposite of this one: an entry with no id kept its dead address whenever the
# candidate reported an id. The reasoning was that a console we CAN name must be different from an
# entry we cannot. IT DOES NOT FOLLOW - the entry has no id to compare against, so the candidate's id
# says nothing about the entry; it only says the candidate runs a build new enough to have one, which
# is now every build. So that guard quietly disabled the no-id fallback entirely the moment consoles
# started reporting ids.
#
# MEASURED ON THE OWNER'S SECOND PC, which is how it was found. Both PCs ran identical builds. One had
# ps4_ip 10.0.0.86 and worked; the other had 10.0.0.87 and showed the PS4 offline for ever, because its
# PS4 entry had no id (an id is only ever learned from a console you can already REACH, and that one
# had never been reachable since ids existed - chicken and egg, and permanent). It could not follow
# (no id to match), and could not adopt (a ps4 entry already existed).
#
# What the old guard was really protecting - a house with a second console of the same platform - is
# still protected by the two cases below it: exactly one unaccounted-for console of that platform, and
# never one whose id already belongs to another entry.
case("an entry with no id follows the only console of its platform",
     [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.250"}],
     {"10.0.0.99": ("ps5", "aaaa000000000001")},
     [("ps5", "10.0.0.99")], 1)

# ---- THE CASE THAT WAS ACTUALLY BROKEN, in the owner's own shape: a PS4 entry written before ids,
# a stale address, and the real PS4 answering elsewhere WITH an id nobody claims.
case("the PS4 that moved is followed even though only IT has an id",
     [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.99", "console_id": "25301f1102048700"},
      {"id": "ps4", "platform": "ps4", "ip": "10.0.0.87"}],
     {"10.0.0.99": ("ps5", "25301f1102048700"),
      "10.0.0.86": ("ps4", "d125ea52a73e6877")},
     [("ps5", "10.0.0.99"), ("ps4", "10.0.0.86")], 1)

# ---- ...and the ambiguity guard still holds when the candidates DO have ids. Two unaccounted-for
# PS4s is unknowable however well each one can name itself.
case("two identifiable PS4s and an entry with no id - address left alone",
     [{"id": "ps4", "platform": "ps4", "ip": "10.0.0.87"}],
     {"10.0.0.86": ("ps4", "bbbb000000000002"), "10.0.0.88": ("ps4", "cccc000000000003")},
     [("ps4", "10.0.0.87")], 0)

# ---- ...and a candidate whose id is already another entry's is still never stolen, even though the
# silent entry has no id of its own to argue with.
case("an id that belongs to another entry is not taken by an entry with none",
     [{"id": "ps4", "platform": "ps4", "ip": "10.0.0.87"},
      {"id": "ps4-1", "platform": "ps4", "ip": "10.0.0.90", "console_id": "cccc000000000003"}],
     {"10.0.0.90": ("ps4", "cccc000000000003")},
     [("ps4", "10.0.0.87"), ("ps4-1", "10.0.0.90")], 0)

# ---- NO SCAN WHEN NOTHING IS MISSING. A PS4 is sitting there unadopted and that is deliberate:
# every console answers, so there is no reason to sweep the network, and a sweep is 254 connects.
case("no console is silent, so the network is not swept at all",
     [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.99", "console_id": "aaaa000000000001"}],
     {"10.0.0.99": ("ps5", "aaaa000000000001"), "10.0.0.86": ("ps4", "bbbb000000000002")},
     [("ps5", "10.0.0.99")], 0)

# ---- ...but once something IS missing, the sweep happens and the new platform is adopted.
case("a never-seen platform is adopted when a sweep does happen",
     [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.11", "console_id": "aaaa000000000001"}],
     {"10.0.0.99": ("ps5", "aaaa000000000001"), "10.0.0.86": ("ps4", "bbbb000000000002")},
     [("ps5", "10.0.0.99"), ("ps4", "10.0.0.86")], 2)

bad = [n for n, ok in results if not ok]
print("\n%d case(s), %d failure(s)" % (len(results), len(bad)))
sys.exit(1 if bad else 0)

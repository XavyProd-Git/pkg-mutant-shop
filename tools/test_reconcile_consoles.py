# -*- coding: utf-8 -*-
"""reconcile_consoles: the settings fields folded into the fleet list, without losing a console.

Every case here is a defect that was real. The function turns the two address fields the settings
panel offers into cfg["consoles"], and it used to:

  * delete a PS5 that genuinely lived at the old shipped example address, because the address
    equalled the default and "equals the default" was read as "not configured";
  * claim EVERY entry of a platform, so clearing one address removed a second console someone had
    added by hand - which SETUP-REMOTE.md tells people to do;
  * append a SECOND entry for a console already in the list under its own id. The install queue
    serialises by console id, so one console under two ids runs two installs against it at once.

Pure function, no console and no network needed.

    python tools/test_reconcile_consoles.py
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


def case(name, cfg, expect):
    cfg.setdefault("ftp", {"port": 2121})
    srv.reconcile_consoles(cfg)
    got = [(c.get("id"), c.get("platform"), c.get("ip")) for c in cfg["consoles"]]
    ok = got == expect
    results.append((name, ok))
    print("  [%s] %s%s" % ("PASS" if ok else "FAIL", name,
                           "" if ok else "\n        got  %s\n        want %s" % (got, expect)))


case("a single PS5 gives one entry, id ps5, first",
     {"ps5_ip": "10.0.0.99", "ps4_ip": "", "consoles": []},
     [("ps5", "ps5", "10.0.0.99")])

case("a PS5 that really lives at the old example address is kept",
     {"ps5_ip": "192.168.1.50", "ps4_ip": "10.0.0.87", "consoles": []},
     [("ps5", "ps5", "192.168.1.50"), ("ps4", "ps4", "10.0.0.87")])

case("a hand-written entry at the same address stays one console, keeping its id",
     {"ps5_ip": "10.0.0.99", "ps4_ip": "", "consoles": [{"id": "living", "ip": "10.0.0.99"}]},
     [("living", "ps5", "10.0.0.99")])

case("clearing the PS5 address removes only the entry this setting owns",
     {"ps5_ip": "", "ps4_ip": "",
      "consoles": [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.99"},
                   {"id": "spare", "platform": "ps5", "ip": "10.0.0.55"}]},
     [("spare", "ps5", "10.0.0.55")])

case("both consoles, and the PS5 sorts first",
     {"ps5_ip": "10.0.0.99", "ps4_ip": "10.0.0.87", "consoles": []},
     [("ps5", "ps5", "10.0.0.99"), ("ps4", "ps4", "10.0.0.87")])

case("running it twice changes nothing",
     {"ps5_ip": "10.0.0.99", "ps4_ip": "10.0.0.87",
      "consoles": [{"id": "ps5", "name": "PS5", "ip": "10.0.0.99", "platform": "ps5", "ftp_port": 2121},
                   {"id": "ps4", "name": "PS4", "ip": "10.0.0.87", "platform": "ps4", "ftp_port": 2121}]},
     [("ps5", "ps5", "10.0.0.99"), ("ps4", "ps4", "10.0.0.87")])

case("a PS4 on its own is still id ps4",
     {"ps5_ip": "", "ps4_ip": "10.0.0.87", "consoles": []},
     [("ps4", "ps4", "10.0.0.87")])

case("an address that moved updates the entry rather than adding one",
     {"ps5_ip": "10.0.0.98", "ps4_ip": "",
      "consoles": [{"id": "ps5", "platform": "ps5", "ip": "10.0.0.99"}]},
     [("ps5", "ps5", "10.0.0.98")])

# THE RULE THAT HAS BEEN GOT WRONG ONCE AND FIXED TWICE.
#
# "A console that does not state a platform is a PS5" holds everywhere else in the companion, and
# the sort key did not follow it: an entry with no `platform` sorted BEHIND the PS4, which put the
# PS4 at consoles[0]. Dozens of routes still mean "the console" by consoles[0], so on a fleet whose
# PS5 was added by hand - no platform field - the mods panel read the PS4 for every title and said
# "no cheat file" while the PS5 beside it held the whole library.
#
# Two independent passes over this file later found and fixed the same line, which is the clearest
# possible sign it should have been pinned by a test the first time. It is now.
# ps4_ip is set so the PS4 entry survives - clearing an address removes the entry that setting
# owns, which is the behaviour two cases above. `living` states no platform and no setting owns it,
# so it keeps platform None: the point is WHERE IT SORTS, not what reconcile stamps on it.
case("an entry that states no platform sorts with the PS5, not behind the PS4",
     {"ps5_ip": "", "ps4_ip": "10.0.0.87",
      "consoles": [{"id": "ps4", "platform": "ps4", "ip": "10.0.0.87"},
                   {"id": "living", "ip": "10.0.0.99"}]},
     [("living", None, "10.0.0.99"), ("ps4", "ps4", "10.0.0.87")])

# And the ordering must be STABLE for entries that DO state one, or a config written through the
# Settings panel would come back reshuffled every time it was read.
case("two stated PS5s keep the order they were written in",
     {"ps5_ip": "", "ps4_ip": "",
      "consoles": [{"id": "lounge", "platform": "ps5", "ip": "10.0.0.99"},
                   {"id": "spare", "platform": "ps5", "ip": "10.0.0.55"}]},
     [("lounge", "ps5", "10.0.0.99"), ("spare", "ps5", "10.0.0.55")])

bad = [n for n, ok in results if not ok]
print("\n%d checks, %d failed" % (len(results), len(bad)))
sys.exit(1 if bad else 0)

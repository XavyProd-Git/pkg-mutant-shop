# -*- coding: utf-8 -*-
"""Gate: which console tabs the Mods & Patches section offers, per game.

WHY THIS IS A GATE AND NOT A LOOK. The rule "a PS5 game has no PS4 tab" is one line inside
modsTabConsoles(), and getting it wrong is invisible on the machine you are testing from - it only
shows up as a tab offering a console the game can never be installed on, on somebody else's fleet,
for one class of title. It is also exactly the kind of thing a later edit to eligibleConsoles()
would silently change, because modsTabConsoles() deliberately defers to it rather than repeating
the rule.

So the functions are pulled out of the page and RUN, against a fake fleet, with the answers
asserted. Not a grep for the source text - a grep cannot tell you that PPSA gets one tab.
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web", "index.html")

# Everything modsTabConsoles() reaches, transitively. Named rather than sliced by line number so
# the gate survives the page growing.
WANT = ["gamePlat", "isBackupTitle", "installsAsBackup", "eligibleConsoles", "modsTabConsoles"]


def grab(src, name):
    """The text of one top-level `function name(...)  { ... }`, matched by brace depth."""
    m = re.search(r"^function %s\(" % re.escape(name), src, re.M)
    if not m:
        raise SystemExit("test_mods_tabs: %s() is gone from the page" % name)
    i = src.index("{", m.start())
    depth, j = 0, i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return src[m.start():j + 1]
        j += 1
    raise SystemExit("test_mods_tabs: %s() never closes" % name)


def main():
    src = io.open(WEB, encoding="utf-8").read()

    mf = re.search(r"^var MOUNT_FMTS=\{[^}]*\};", src, re.M)
    if not mf:
        raise SystemExit("test_mods_tabs: MOUNT_FMTS is gone")

    parts = [mf.group(0)] + [grab(src, n) for n in WANT]

    js = "\n".join(parts) + r"""
var state={consoles:[]};
function run(consoles,game){ state.consoles=consoles;
  return modsTabConsoles(game).map(function(x){return x.plat;}); }
var BOTH=[{id:"ps5",name:"PS5",platform:"ps5"},{id:"ps4",name:"PS4",platform:"ps4"}];
var ONLY5=[{id:"ps5",name:"PS5",platform:"ps5"}];
var ONLY4=[{id:"ps4",name:"PS4",platform:"ps4"}];
var out={
  ps4_game_both:   run(BOTH,{title_id:"CUSA58072",name:"Bluey"}),
  ps5_game_both:   run(BOTH,{title_id:"PPSA01234",name:"A PS5 game"}),
  backup_both:     run(BOTH,{title_id:"CUSA11111",name:"A backup",source:"backup"}),
  unknown_both:    run(BOTH,{title_id:"",name:"No title id"}),
  ps4_game_one5:   run(ONLY5,{title_id:"CUSA58072",name:"Bluey"}),
  ps4_game_one4:   run(ONLY4,{title_id:"CUSA58072",name:"Bluey"})
};
console.log(JSON.stringify(out));
"""

    p = subprocess.run([os.environ.get("NODE", "node"), "-e", js],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit("test_mods_tabs: the tab logic threw\n" + (p.stderr or "")[:2000])
    got = json.loads(p.stdout.strip())

    want = {
        # A PS4 game with both consoles connected: a real choice, PS5 first (it has the engine).
        "ps4_game_both": ["ps5", "ps4"],
        # THE ONE THE OWNER REPORTED. A PPSA title cannot be on a PS4, so no PS4 tab - and with
        # one tab left the strip is not drawn at all and the section looks as it always did.
        "ps5_game_both": ["ps5"],
        # A mounted backup is a PS5 arrangement whatever its title id says.
        "backup_both": ["ps5"],
        # No title id is the permissive case everywhere else in this app; it stays permissive.
        "unknown_both": ["ps5", "ps4"],
        # One console is never a choice, so there is never a strip.
        "ps4_game_one5": ["ps5"],
        "ps4_game_one4": ["ps4"],
    }

    bad = []
    for k in sorted(want):
        if got.get(k) != want[k]:
            bad.append("  %-16s got %-16s want %s" % (k, got.get(k), want[k]))
    if bad:
        print("test_mods_tabs: FAIL")
        print("\n".join(bad))
        return 1

    print("test_mods_tabs: OK (%d cases; a PS5 game offers no PS4 tab)" % len(want))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Gate: the mods panel names the console it is actually about - including a PS4.

WHY THIS IS A GATE. modsConsole() decides which console every read and write in the Mods & Patches
section goes to, and it is one line. That line used to read

    return consolePlat(id)==="ps4" ? "" : id;

which was RIGHT when the PS4 payload answered every cheat route with "not available on the PS4
yet": naming it sent the request to a machine that could only refuse, and leaving it unnamed let
the PC resolve one that could serve it. Once the PS4 had an engine, the same line meant that on a
two-console fleet A PS4 GAME'S TOGGLES WERE SENT TO THE PS5 - the console the request was about was
the one id the request was forbidden to carry. Nothing failed loudly; the PS5 answered, about a
game it was not running.

That is invisible on a single-console setup, which is what most testing happens on, and it is one
edit away from coming back the next time somebody writes a PS4 special case. So the function is
pulled out of the page and RUN, against a fake fleet, with the answers asserted - a grep cannot
tell you which console id came out the other end.

The empty answers are checked too, because "" is meaningful here: it means "let the PC decide",
which is right for "all" and for an id from a fleet that has since been reconfigured, and wrong
for every real console.
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web", "index.html")

WANT = ["consolePlat", "modsConsole", "modsConsoleQ"]


def grab(src, name):
    """The text of one top-level `function name(...) { ... }`, matched by brace depth."""
    m = re.search(r"^function %s\(" % re.escape(name), src, re.M)
    if not m:
        raise SystemExit("test_mods_console: %s() is gone from the page" % name)
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
    raise SystemExit("test_mods_console: %s() never closes" % name)


def main():
    src = io.open(WEB, encoding="utf-8").read()

    # panelConsole() reads the DOM (the panel's own picker) and is not what this is about, so it is
    # replaced by a stub that returns whatever the case says the panel is aimed at. Everything
    # below it - the part that decides whether that answer is carried on the wire - is the real
    # page code.
    js = "\n".join(grab(src, n) for n in WANT) + r"""
var state={consoles:[],consoleById:{},console:""};
var PANEL="";
function panelConsole(g){ return PANEL; }
function run(consoles,panel){
  state.consoles=consoles; state.consoleById={};
  consoles.forEach(function(c){state.consoleById[c.id]=c;});
  PANEL=panel;
  return [modsConsole(null), modsConsoleQ(null), modsConsoleQ(null,"&")];
}
var FLEET=[{id:"ps5",platform:"ps5"},{id:"ps4",platform:"ps4"}];
var ONLY5=[{id:"ps5",platform:"ps5"}];
var ONLY4=[{id:"ps4",platform:"ps4"}];
var CASES=[
  ["a PS4 panel on a two-console fleet names the PS4", FLEET, "ps4",
   ["ps4","?console=ps4","&console=ps4"]],
  ["a PS5 panel on a two-console fleet names the PS5", FLEET, "ps5",
   ["ps5","?console=ps5","&console=ps5"]],
  ["a lone PS4 is still named", ONLY4, "ps4", ["ps4","?console=ps4","&console=ps4"]],
  ["a lone PS5 is still named", ONLY5, "ps5", ["ps5","?console=ps5","&console=ps5"]],
  ["\"all\" is not a console, so the PC decides", FLEET, "all", ["","",""]],
  ["an id from a fleet that has been reconfigured is not sent", FLEET, "ps3", ["","",""]],
  ["no panel console at all", FLEET, "", ["","",""]]
];
var out=[];
CASES.forEach(function(c){
  var got=run(c[1],c[2]);
  out.push({name:c[0],want:c[3],got:got,ok:JSON.stringify(got)===JSON.stringify(c[3])});
});
console.log(JSON.stringify(out));
"""

    try:
        p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        print("test_mods_console: node is not installed - skipping")
        return 0
    if p.returncode != 0:
        print(p.stderr.strip()[:1500])
        return 1

    rows = json.loads(p.stdout.strip().splitlines()[-1])
    bad = 0
    for r in rows:
        if not r["ok"]:
            bad += 1
        print("  [%s] %s%s" % ("PASS" if r["ok"] else "FAIL", r["name"],
                               "" if r["ok"] else "  -> got %r want %r" % (r["got"], r["want"])))
    print("test_mods_console: %s (%d case%s)"
          % ("OK" if not bad else "%d FAILURE(S)" % bad, len(rows), "" if len(rows) == 1 else "s"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

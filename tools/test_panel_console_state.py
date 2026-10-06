# -*- coding: utf-8 -*-
"""Gate: the game panel's facts describe the CHOSEN console, not the fleet.

THE DEFECT THIS EXISTS FOR WAS REAL AND REPORTED. Grounded (CUSA42556) sits on both consoles, but
only the PS5 had update 01.14 - the PS4 was on 01.00. With the PS4 selected the panel showed that
update as already "Installed", so it could not be installed onto the PS4 at all. The header was
right at the same moment ("PS4 (update waiting)"), which is what gives the shape of the bug away:
the per-console answer already existed and a handful of places were still reading the fleet one.

The fleet-wide fields (g.installed_version, g.console_size, g.installed_drive, g.on_console) are
whichever console the library builder happened to see first. They are correct for the GRID, where
"is this installed anywhere" is the question. They are wrong for the panel, which is aimed at one
machine and is about to install onto it.

So the helpers are pulled out of the page and RUN against a two-console fleet. A grep cannot tell
you that picking the PS4 changes the answer; running it can.
"""
import io
import json
import os
import re
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
WEB = os.path.join(os.path.dirname(HERE), "web", "index.html")

# Everything the panel's per-console answers reach, transitively.
WANT = ["verGT", "onConsoleFor", "consoleStateOf", "flatFieldIsFor", "installedDriveOn",
        "installedVersionOn", "installedSizeOn", "pendingUpdates", "stateOf", "stateOfOn",
        # The grid's own answer. Added when the owner said "do what is best for the grid": with a
        # platform filter naming one console, a card answers about THAT console - the same
        # stateOfOn() the panel uses, so a card and the panel it opens cannot disagree.
        "gridConsole", "gridStateOf"]


def grab(src, name):
    """The text of one top-level `function name(...) { ... }`, matched by brace depth."""
    m = re.search(r"^function %s\(" % re.escape(name), src, re.M)
    if not m:
        raise SystemExit("test_panel_console_state: %s() is gone from the page" % name)
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
    raise SystemExit("test_panel_console_state: %s() never closes" % name)


def main():
    src = io.open(WEB, encoding="utf-8").read()

    # THE PANEL'S OWN LINES, verified to still exist rather than reimplemented here. If the panel
    # stops using the per-console helper, this gate must fail even though the helper still works.
    required = [
        ("the update row is driven by this console's pending list",
         "var _pend=pendingUpdates(g,_cid);"),
        ("...and the badge asks that list, not the fleet version",
         "if(_pend.indexOf(it)<0)"),
        ("the VERSION pill is this console's", "var verTxt0=installedVersionOn(g,_cid)"),
        ("the INSTALLED TO pill is this console's", "instTxt=_onHere?(installedDriveOn(g,_cid)"),
        ("the size pill is this console's", "var _szHere=installedSizeOn(g,_cid);"),
        ("the console-only base row is this console's", "} else if(onConsoleFor(g,_cid)){"),
        # A card that went back to stateOf() would silently undo the grid decision.
        ("the grid asks its own question", "var st=gridStateOf(g);"),
        # FOUR NOTES CAME OUT OF THE MODS SECTION AND THEIR BRANCHES STAYED. Removing a note by
        # collapsing its condition hands the case to the arm below - which is how the master note
        # once stopped a title being told to install the game - so the SHAPE is what is pinned
        # here, not the absence. Perturb either line and this goes red.
        ("the not-running arm still consumes the case",
         'else if(!r.running){ if(!mods.length) addNote(body,t("gp_patches_listed_only")); }'),
        ("an armed PS4 title draws neither note nor button",
         "if(r.helper_armed!==true){"),
        ("the mismatch arm still consumes its own case",
         "if(!okv){ /* nothing to add"),
    ]
    missing = [why for why, needle in required if needle not in src]

    # ...AND THE SENTENCES THEMSELVES DO NOT COME BACK. The owner asked for all four to go: the
    # header already carries the version mismatch, the fourth duplicated the version picker under it,
    # and the other two told them to do something the panel does for them. The dictionary entries
    # stay (dead weight, not an error) - it is the CALL that must not return.
    gone = [
        ("the version-mismatch paragraph", 'tsub("gp_mods_mismatch_note"'),
        ("the helper-already-on note", 't("gp_mod_helper_ready")'),
        ("the mods-listed-only note", 't("gp_mods_listed_only")'),
        # The fourth one: written as the tail of the mismatch paragraph, so with the paragraph gone
        # it stood alone above the version picker and duplicated it.
        ("the other-versions line", 'tsub("gp_mods_other_versions"'),
    ]
    missing += ["%s is back in the panel" % why for why, needle in gone if needle in src]

    if missing:
        print("test_panel_console_state: FAIL - the panel stopped asking per console")
        for m in missing:
            print("  missing: %s" % m)
        return 1

    js = "\n".join(grab(src, n) for n in WANT) + r"""
var state={installed:new Set()};
/* Grounded, as the owner actually has it: on BOTH consoles, but the PS5 took update 01.14 and the
   PS4 is still on 01.00. One update file exists in the library. */
var UPD = {version:"01.14", file:"Maine-CUSA42556-UPDATE.pkg"};
var G = {
  title_id:"CUSA42556", name:"Maine", size:9600000000,
  on_console:true, installed_version:"01.14", installed_drive:"Internal SSD",
  console_size:9600000000,
  installed_on:["ps5","ps4"],
  console_state:{
    ps5:{drive:"Internal SSD", version:"01.14", size:9600000000},
    ps4:{drive:"Internal SSD", version:"01.00", size:4900000000}
  },
  updates:[UPD], dlc:[]
};
/* A game on the PS5 ONLY - the panel must not claim it is on the PS4. */
var P5 = {
  title_id:"CUSA00001", name:"PS5 only", on_console:true,
  installed_version:"01.00", installed_on:["ps5"],
  console_state:{ps5:{drive:"Internal SSD", version:"01.00", size:1}},
  updates:[], dlc:[]
};
/* THE LEAK THE ADVERSARIAL PASS FOUND. On both consoles, but the PS4's app.db has no APP_VER row,
   so console_state.ps4.version is empty. The flat field is the PS5's 01.14, and returning it here is
   what put "Installed" on the badge with the PS4 selected - the original bug, from inside the
   per-console path. Unknown must stay unknown, and unknown means "offer the update". */
var NOVER = {
  title_id:"CUSA42557", name:"No app_ver on the PS4", on_console:true,
  installed_version:"01.14", installed_drive:"Internal SSD", console_size:999,
  installed_on:["ps5","ps4"],
  console_state:{ps5:{drive:"Internal SSD", version:"01.14", size:999}, ps4:{drive:"", version:"", size:0}},
  updates:[{version:"01.14"}], dlc:[]
};
/* ...but on a ONE-console fleet the flat field is that console's, and must still be used. */
var SOLO = {
  title_id:"CUSA42558", name:"Only console", on_console:true,
  installed_version:"01.14", installed_drive:"Internal SSD", console_size:777,
  installed_on:["ps4"], console_state:{}, updates:[{version:"01.14"}], dlc:[]
};
function pend(g,cid){ return pendingUpdates(g,cid).length; }
/* THE GRID DECISION. state.plat is what the filter buttons set, and state.consoles is the fleet the
   library reply carried. Four situations, and only one of them may change the old answer. */
function gridAnswer(plat, consoles, g){
  state.plat = plat; state.consoles = consoles;
  return [gridConsole(), gridStateOf(g)];
}
var TWO = [{id:"ps5", platform:"ps5"}, {id:"ps4", platform:"ps4"}];
/* A title the CONSOLE has just reported as installed, with no per-console record in the library yet.
   stateOf() counts state.installed; stateOfOn() cannot see it - so the grid must fall back rather
   than say "not installed" about a game that is sitting on the console. */
var LIVEONLY = {title_id:"CUSA99999", name:"Just installed", on_console:false,
                installed_version:"", installed_on:[], console_state:{}, updates:[], dlc:[]};
var ONE = [{id:"ps4", platform:"ps4"}];
var TWO_PS5 = [{id:"ps5-0", platform:"ps5"}, {id:"ps5-1", platform:"ps5"}];
console.log(JSON.stringify({
  ps5_state:   stateOfOn(G,"ps5"),
  ps4_state:   stateOfOn(G,"ps4"),
  ps5_pending: pend(G,"ps5"),
  ps4_pending: pend(G,"ps4"),
  ps5_version: installedVersionOn(G,"ps5"),
  ps4_version: installedVersionOn(G,"ps4"),
  ps5_size:    installedSizeOn(G,"ps5"),
  ps4_size:    installedSizeOn(G,"ps4"),
  fleet_state: stateOf(G),
  p5_on_ps5:   onConsoleFor(P5,"ps5"),
  p5_on_ps4:   onConsoleFor(P5,"ps4"),
  p5_ver_ps4:  installedVersionOn(P5,"ps4"),
  p5_size_ps4: installedSizeOn(P5,"ps4"),
  nover_ps4_version: installedVersionOn(NOVER,"ps4"),
  nover_ps4_drive:   installedDriveOn(NOVER,"ps4"),
  nover_ps4_pending: pend(NOVER,"ps4"),
  nover_ps5_pending: pend(NOVER,"ps5"),
  solo_version:      installedVersionOn(SOLO,"ps4"),
  solo_pending:      pend(SOLO,"ps4"),
  grid_all:          gridAnswer("all", TWO, G),
  grid_ps5:          gridAnswer("ps5", TWO, G),
  grid_ps4:          gridAnswer("ps4", TWO, G),
  grid_ps4_only_p5:  gridAnswer("ps4", TWO, P5),
  grid_pc:           gridAnswer("PC", TWO, G),
  grid_one_console:  gridAnswer("ps4", ONE, SOLO),
  grid_two_same:     gridAnswer("ps5", TWO_PS5, G),
  /* the live installed set, with a platform tab selected and no per-console data to go on */
  grid_liveonly_ps4: (function(){ state.installed=new Set(["CUSA99999"]);
                                  var r=gridAnswer("ps4", TWO, LIVEONLY);
                                  state.installed=new Set(); return r; })(),
  /* ...and with per-console data that says it is NOT on that console, the per-console answer wins */
  grid_ps5only_ps4:  (function(){ state.installed=new Set(["CUSA00001"]);
                                  var r=gridAnswer("ps4", TWO, P5);
                                  state.installed=new Set(); return r; })()
}));
"""

    p = subprocess.run([os.environ.get("NODE", "node"), "-e", js],
                       capture_output=True, text=True)
    if p.returncode != 0:
        raise SystemExit("test_panel_console_state: the panel logic threw\n" + (p.stderr or "")[:2000])
    got = json.loads(p.stdout.strip())

    want = {
        # The PS5 has 01.14: nothing outstanding, so the row shows "Installed" there.
        "ps5_state": "inst", "ps5_pending": 0, "ps5_version": "01.14",
        # THE ONE THE OWNER REPORTED. The PS4 is on 01.00, so 01.14 is still OUTSTANDING there and
        # the row must offer the Install button instead of claiming it is done.
        "ps4_state": "upd", "ps4_pending": 1, "ps4_version": "01.00",
        # Sizes come from the same per-console record, so the fact sheet cannot print the other
        # console's number.
        "ps5_size": 9600000000, "ps4_size": 4900000000,
        # stateOf() IS STILL THE FLEET ANSWER, and must stay one: it reads g.installed_version,
        # which here is the PS5's 01.14, so it says "installed" even though the PS4 wants that
        # update. That is its documented contract and the grid falls back to it whenever the filter
        # does not name a single console - see the grid_* answers below, which are the per-console
        # half the owner asked for.
        "fleet_state": "inst",
        # A PS5-only game is not on the PS4, and has no version or size there to print.
        "p5_on_ps5": True, "p5_on_ps4": False, "p5_ver_ps4": "", "p5_size_ps4": 0,
        # THE LEAK: this console's version is unknown, so it must NOT borrow the other's. Unknown
        # means every update is still outstanding, which offers the install instead of hiding it.
        "nover_ps4_version": "", "nover_ps4_drive": "", "nover_ps4_pending": 1,
        # ...while the console that DOES report a version is still answered exactly.
        "nover_ps5_pending": 0,
        # ...and on a one-console fleet the flat field is that console's, so it is still used.
        "solo_version": "01.14", "solo_pending": 0,
        # ---- THE GRID DECISION ------------------------------------------------------------------
        # The owner asked for "whatever is best for the grid". This is it: the badge answers the
        # question the filter asks. Each pair is [which console the grid is about, the badge state].
        #
        # On "All" the question is about the household, so the fleet answer stands - which for
        # Grounded means "inst", even though the PS4 is a version behind. That is not a bug: with no
        # platform chosen there is no one machine to be behind.
        "grid_all": ["", "inst"],
        # Filter to the PS5, which has 01.14: nothing outstanding.
        "grid_ps5": ["ps5", "inst"],
        # Filter to the PS4, which is on 01.00: THE CARD NOW SAYS "Update", which is the whole point.
        # Before this it said "Installed" while the panel one tap away said an update was waiting.
        "grid_ps4": ["ps4", "upd"],
        # A PS5-only game, with the PS4 filter on: not installed there, and the card says so.
        "grid_ps4_only_p5": ["ps4", "none"],
        # PC and USB are not consoles, so they keep the fleet answer.
        "grid_pc": ["", "inst"],
        # ONE CONSOLE CONFIGURED: the per-console answer and the fleet answer are the same thing, so
        # nothing changes for a single-console household.
        "grid_one_console": ["ps4", "inst"],
        # TWO CONSOLES OF THE SAME PLATFORM: the filter does not name one of them, so it must not
        # guess - back to the fleet answer.
        "grid_two_same": ["", "inst"],
        # THE LIVE INSTALLED SET. No installed_on and no console_state, so there is nothing to be
        # per-console about: the fleet answer is the honest one, and it counts state.installed.
        # Without this the grid said "not installed" about a game the console had just reported.
        "grid_liveonly_ps4": ["ps4", "inst"],
        # ...but when the library DOES carry per-console data saying it is not on that console, the
        # per-console answer wins even though the live set names it.
        "grid_ps5only_ps4": ["ps4", "none"],
    }

    bad = []
    for k in sorted(want):
        if got.get(k) != want[k]:
            bad.append("  %-12s got %-14r want %r" % (k, got.get(k), want[k]))
    if bad:
        print("test_panel_console_state: FAIL")
        print("\n".join(bad))
        return 1

    print("test_panel_console_state: OK (%d answers + %d panel call sites + %d removed notes "
          "still gone; an update on the PS5 is still pending on the PS4)"
          % (len(want), len(required), len(gone)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

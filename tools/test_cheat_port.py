# -*- coding: utf-8 -*-
"""What cheat_port.py must refuse, which is most of what it does.

A porting tool that emits something for every input is worse than no tool: a plausible-looking cheat
file with offsets belonging to another build produces a refusal from the engine at best, and at worst
somebody forces it. So the interesting tests are the negative ones, and they are the ones here.

Fixtures are built in a temporary directory rather than taken from the shipped library, because a
library file can change and a test that changes with it proves nothing. Two real cases are checked
against the library as well, and they are stated as "this is what it is today" rather than as rules.

    python tools/test_cheat_port.py            # run
    python tools/test_cheat_port.py --check    # same, exit 1 on failure (what the gates call)
"""
import argparse
import io
import json
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
TOOL = os.path.join(HERE, "cheat_port.py")

FAIL = []
RAN = [0]


def ok(cond, what, extra=""):
    RAN[0] += 1
    if not cond:
        FAIL.append(what)
        print("  FAIL  %s%s" % (what, ("   [%s]" % extra[:400]) if extra else ""))


def mod(name, *entries):
    return {"name": name, "type": "checkbox",
            "memory": [dict(zip(("offset", "on", "off"), e)) for e in entries]}


def doc(tid, ver, mods, master=None):
    d = {"name": "T", "id": tid, "version": ver, "process": "eboot.bin", "mods": mods,
         "credits": "test"}
    if master:
        d["master"] = master
    return d


def run(tmp, *args):
    """cheat_port with its library pointed at the fixture directory.

    The tool reads assets/cheats/json relative to its own location, so the fixture is a whole little
    tree - which also proves the tool does not reach outside it."""
    env = dict(os.environ)
    r = subprocess.run([sys.executable, os.path.join(tmp, "tools", "cheat_port.py")] + list(args),
                       capture_output=True, text=True, env=env, cwd=tmp)
    return r.returncode, (r.stdout or "") + (r.stderr or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    ap.parse_args()

    tmp = tempfile.mkdtemp(prefix="pms_port_")
    try:
        cj = os.path.join(tmp, "assets", "cheats", "json")
        os.makedirs(cj)
        os.makedirs(os.path.join(tmp, "tools"))
        shutil.copy(TOOL, os.path.join(tmp, "tools", "cheat_port.py"))

        def put(name, d):
            io.open(os.path.join(cj, name), "w", encoding="utf-8", newline="\n").write(
                json.dumps(d, indent=1))

        print("a clean port, with agreeing anchors")
        # three anchors all moved by +0x100, and one mod missing from the target
        A = doc("CUSA00001", "01.00", [
            mod("Anchor One", ("1000", "9090", "7405")),
            mod("Anchor Two", ("2000", "90909090", "41890470")),
            mod("Anchor Three", ("3000", "B801000000", "8B01000000")),
            mod("Only In A", ("4000", "9090", "7510")),
        ])
        B = doc("CUSA00001", "01.01", [
            mod("Anchor One", ("1100", "9090", "7405")),
            mod("Anchor Two", ("2100", "90909090", "41890470")),
            mod("Anchor Three", ("3100", "B801000000", "8B01000000")),
        ])
        put("CUSA00001_01.00.json", A)
        put("CUSA00001_01.01.json", B)
        rc, out = run(tmp, "--title", "CUSA00001", "--from", "01.00", "--to", "01.01")
        ok(rc == 0, "a clean port succeeds", out)
        ok("3 anchor(s), 1 distinct delta(s)" in out, "it finds all three anchors", out)
        ok("+0x100" in out, "it reports the delta", out)
        ok("PORTABLE  Only In A" in out, "the missing mod is portable", out)
        ok("-> 4100" in out, "and lands at the source offset plus the delta", out)
        ok("nothing was written" in out, "nothing is written without being asked", out)

        rc, out = run(tmp, "--title", "CUSA00001", "--from", "01.00", "--to", "01.01",
                      "--out", os.path.join(tmp, "o.json"))
        ok(rc == 0, "--out writes", out)
        w = json.load(io.open(os.path.join(tmp, "o.json"), encoding="utf-8"))
        ok(len(w["mods"]) == 4, "the written file has the target's mods plus the ported one",
           str(len(w["mods"])))
        ok(w["mods"][-1]["memory"][0]["offset"] == "4100", "with the new offset",
           json.dumps(w["mods"][-1]))
        ok(w["version"] == "01.01", "and the TARGET's version, not the source's", w.get("version"))
        src = json.load(io.open(os.path.join(cj, "CUSA00001_01.00.json"), encoding="utf-8"))
        ok(src["mods"][3]["memory"][0]["offset"] == "4000", "the source file is untouched",
           json.dumps(src["mods"][3]))

        print("anchors that disagree produce nothing")
        A2 = doc("CUSA00002", "01.00", [
            mod("Anchor One", ("1000", "9090", "7405")),
            mod("Anchor Two", ("2000", "90909090", "41890470")),
            mod("Only In A", ("4000", "9090", "7510")),
        ])
        B2 = doc("CUSA00002", "01.01", [
            mod("Anchor One", ("1100", "9090", "7405")),
            mod("Anchor Two", ("2222", "90909090", "41890470")),   # a different delta
        ])
        put("CUSA00002_01.00.json", A2)
        put("CUSA00002_01.01.json", B2)
        rc, out = run(tmp, "--title", "CUSA00002", "--from", "01.00", "--to", "01.01")
        ok(rc != 0, "disagreeing anchors fail", out)
        ok("THE ANCHORS DISAGREE" in out, "and say so", out)
        ok("PORTABLE" not in out, "and offer nothing", out)

        print("no anchors at all produces nothing")
        put("CUSA00003_01.00.json", doc("CUSA00003", "01.00", [mod("A", ("1000", "90", "74"))]))
        put("CUSA00003_01.01.json", doc("CUSA00003", "01.01", [mod("B", ("2000", "90", "74"))]))
        rc, out = run(tmp, "--title", "CUSA00003", "--from", "01.00", "--to", "01.01")
        ok(rc != 0 and "NO ANCHORS" in out, "no anchors fails and says so", out)

        print("a code cave is never ported")
        A4 = doc("CUSA00004", "01.00", [
            mod("Anchor", ("1000", "9090", "7405")),
            mod("Anchor2", ("1500", "9090", "7405")),
            mod("Cave Hook", ("5000", "4889C8E900000000", "0000000000000000"),
                             ("6000", "E900000000", "4889C84890")),
        ])
        B4 = doc("CUSA00004", "01.01", [mod("Anchor", ("1000", "9090", "7405")),
                                        mod("Anchor2", ("1500", "9090", "7405"))])
        put("CUSA00004_01.00.json", A4)
        put("CUSA00004_01.01.json", B4)
        rc, out = run(tmp, "--title", "CUSA00004", "--from", "01.00", "--to", "01.01")
        ok(rc == 0, "the run itself succeeds", out)
        ok("refused   Cave Hook" in out, "the cave mod is refused", out)
        ok("code cave" in out, "with the reason", out)
        ok("PORTABLE" not in out, "and nothing else is offered", out)

        print("a relative jump is never ported")
        A5 = doc("CUSA00005", "01.00", [
            mod("Anchor", ("1000", "9090", "7405")),
            mod("Anchor2", ("1500", "9090", "7405")),
            mod("Jumper", ("7000", "E91573EE01", "4189877001")),
        ])
        put("CUSA00005_01.00.json", A5)
        put("CUSA00005_01.01.json", doc("CUSA00005", "01.01",
                                        [mod("Anchor", ("1000", "9090", "7405")),
                                         mod("Anchor2", ("1500", "9090", "7405"))]))
        rc, out = run(tmp, "--title", "CUSA00005", "--from", "01.00", "--to", "01.01")
        ok("refused   Jumper" in out and "position-dependent" in out,
           "a mod carrying a rel32 jump is refused", out)

        print("a cheat that patches the master's own routine is never ported")
        # Dark Souls II's real shape: the master installs a routine and the cheats patch inside it.
        A6 = doc("CUSA00006", "01.00", [
            mod("Anchor", ("1000", "9090", "7405")),
            mod("Anchor2", ("1500", "9090", "7405")),
            mod("God mode", ("2077807", "8B8378010000", "8B8370010000")),
        ], master={"challenged": "yes",
                   "memory": [{"offset": "2077800",
                               "on": "48891DF9C7B8FD8B8370010000898370010000"}]})
        put("CUSA00006_01.00.json", A6)
        put("CUSA00006_01.01.json", doc("CUSA00006", "01.01",
                                        [mod("Anchor", ("1000", "9090", "7405")),
                                         mod("Anchor2", ("1500", "9090", "7405"))]))
        rc, out = run(tmp, "--title", "CUSA00006", "--from", "01.00", "--to", "01.01")
        ok("refused   God mode" in out, "a cave-resident cheat is refused", out)
        ok("master code's own routine" in out, "with the reason", out)

        print("one anchor is not enough to write on")
        A7 = doc("CUSA00007", "01.00", [mod("Anchor", ("1000", "9090", "7405")),
                                        mod("Only In A", ("4000", "9090", "7510"))])
        put("CUSA00007_01.00.json", A7)
        put("CUSA00007_01.01.json", doc("CUSA00007", "01.01",
                                        [mod("Anchor", ("1100", "9090", "7405"))]))
        rc, out = run(tmp, "--title", "CUSA00007", "--from", "01.00", "--to", "01.01")
        ok(rc == 0 and "PORTABLE  Only In A" in out, "it still reports what it would do", out)
        rc, out = run(tmp, "--title", "CUSA00007", "--from", "01.00", "--to", "01.01",
                      "--out", os.path.join(tmp, "thin.json"))
        ok(rc != 0 and "ONE ANCHOR ONLY" in out, "but refuses to write on one anchor", out)
        ok(not os.path.exists(os.path.join(tmp, "thin.json")), "and writes no file")
        rc, out = run(tmp, "--title", "CUSA00007", "--from", "01.00", "--to", "01.01",
                      "--force", "--out", os.path.join(tmp, "thin.json"))
        ok(rc == 0 and os.path.exists(os.path.join(tmp, "thin.json")),
           "--force writes anyway, which is what force is for", out)

        print("a delta of one image base is called what it is")
        A8 = doc("CUSA00008", "01.00", [mod("Anchor", ("1000", "9090", "7405")),
                                        mod("Anchor2", ("1500", "9090", "7405"))])
        put("CUSA00008_01.00.json", A8)
        put("CUSA00008_01.01.json", doc("CUSA00008", "01.01",
                                        [mod("Anchor", ("401000", "9090", "7405")),
                                         mod("Anchor2", ("401500", "9090", "7405"))]))
        rc, out = run(tmp, "--title", "CUSA00008", "--from", "01.00", "--to", "01.01")
        ok("NOT a code move" in out, "an image-base delta is not reported as code moving", out)

        print("--by-signature refuses what it cannot place")
        # The search itself needs a running console, so what is tested here is the part that decides
        # WHETHER to search - which is where a wrong answer would be written into a file.
        put("CUSA00009_01.00.json", doc("CUSA00009", "01.00", [
            mod("cave", ("5000", "4889C8E900000000", "0000000000000000")),
            mod("jumper", ("6000", "E91573EE01", "4189877001")),
            mod("too short", ("7000", "9090", "7405")),
            mod("searchable", ("8000", "90909090", "41890470")),
        ]))
        # ...and one that targets another module, which the engine refuses outright
        _d = doc("CUSA00009", "01.00", [])
        _d = json.load(io.open(os.path.join(cj, "CUSA00009_01.00.json"), encoding="utf-8"))
        _d["mods"].append({"name": "sectioned", "type": "checkbox",
                           "memory": [{"section": "11", "offset": "9000",
                                       "on": "90909090", "off": "41890470"}]})
        put("CUSA00009_01.00.json", _d)
        # THE LADDER RUNS BEFORE THE CONSOLE IS NEEDED, so every refusal is assertable here. The
        # previous version of this check was `"not running" in out or "no cheat file" not in out`,
        # whose second half is true of almost any output - it could not fail.
        rc, out = run(tmp, "--by-signature", "--title", "CUSA00009", "--from", "01.00")
        ok("skipped   cave" in out and "code cave" in out, "a cave is skipped, by name", out[:400])
        ok("skipped   jumper" in out and "position-dependent" in out,
           "a relative jump is skipped, by name", out[:400])
        ok("skipped   too short" in out and "too short to search" in out,
           "bytes too short to search for are skipped, by name", out[:400])
        ok("skipped   sectioned" in out and "another loaded module" in out,
           "a sectioned mod is skipped - the engine refuses those outright", out[:400])
        ok("skipped   searchable" not in out, "and the one that CAN be searched is not skipped",
           out[:400])
        ok("not running" in out, "then it asks for the game, because from here it reads it", out[:400])
        ok(rc != 0, "and it does not claim success", str(rc))
        rc, out = run(tmp, "--by-signature", "--title", "CUSA00009")
        ok(rc == 2 and "--from" in out, "and it needs to be told which file", out[:200])

        print("cheat_find's verdicts, which are the whole point of it")
        # Importable without a console: verdict() is the one place that decides what a count means.
        import importlib.util as _il
        _sp = _il.spec_from_file_location("pms_find", os.path.join(HERE, "cheat_find.py"))
        _cf = _il.module_from_spec(_sp)
        _sp.loader.exec_module(_cf)
        ok("ONE MATCH" in _cf.verdict({}, ["5BCBA5"], 0)[0], "one match is an address")
        ok("NOT IN THIS BUILD" in _cf.verdict({}, [], 0)[0],
           "no matches and no gaps means the bytes are not here")
        ok("CANNOT SAY" in _cf.verdict({}, [], 7)[0],
           "NO MATCHES WITH GAPS IS NOT 'NOT HERE' - nothing was read there")
        ok("NOT AN ADDRESS" in _cf.verdict({}, ["1", "2"], 0)[0], "two matches is not an address")
        ok("NOT USABLE" in _cf.verdict({"truncated": True}, ["1"], 0)[0],
           "a truncated list is not an answer even when it holds one")

        print("the shipped library, as it is today")
        # Not rules - measurements. If the library changes these change, and somebody should look.
        # THE REAL FILES ARE COPIED IN. Asking the fixture tree about a title it does not have would
        # fail for the wrong reason and pass this check while proving nothing.
        real = os.path.join(ROOT, "assets", "cheats", "json")
        got = 0
        for n in ("CUSA01589_01.00.json", "CUSA01589_01.02.json"):
            if os.path.isfile(os.path.join(real, n)):
                shutil.copy(os.path.join(real, n), os.path.join(cj, n))
                got += 1
        if got == 2:
            rc, out = run(tmp, "--title", "CUSA01589", "--from", "01.02", "--to", "01.00")
            ok("NO ANCHORS" in out,
               "Dark Souls II has no anchor between 01.02 and 01.00 - its 01.02 cheats patch a cave",
               out[:400])
            ok(rc != 0, "so it refuses", out[:200])
        else:
            print("  (the Dark Souls II files are not here - skipped)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    print("\n%d check(s), %d failure(s)" % (RAN[0], len(FAIL)))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())

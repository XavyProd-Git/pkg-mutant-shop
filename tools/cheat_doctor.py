# -*- coding: utf-8 -*-
"""Does this cheat actually fit the game that is running? And is it safe to port?

WHY THIS EXISTS. The owner enabled "1 hit kill" on Dark Souls II and the game crashed the moment they
hit an enemy - and the honest answer to "is our engine wrong or is the cheat wrong?" was that nobody
could tell, because nothing ever compared a cheat file against the build it was aimed at. This does.

It answers two questions a cheat file cannot answer about itself:

  1. DOES IT FIT?  For every byte run in the file, read that address out of the RUNNING game and say
     whether it holds the documented OFF bytes (ready), the ON bytes (already applied), or neither
     (this file was not built for this executable). Reading is free and changes nothing.

  2. IS IT PORTABLE?  Classify each mod by what it actually does. A cheat that swaps a few bytes in
     place can be moved to another build by finding those bytes again. A cheat that installs a CODE
     CAVE and jumps into it cannot: its jumps carry displacements computed for one executable, and
     the same bytes written into a different build point somewhere else entirely.

    python tools/cheat_doctor.py CUSA02290                 # the running game, its installed version
    python tools/cheat_doctor.py CUSA01589 --version 01.02 # a specific file, offline classification
    python tools/cheat_doctor.py --all                     # classify the whole library, no console
"""
import argparse
import io
import json
import os
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHEATS = os.path.join(ROOT, "assets", "cheats")
PC = "http://127.0.0.1:8710"

# x86-64 opcodes whose operand is a DISPLACEMENT FROM THE INSTRUCTION ITSELF. Any run containing one
# is tied to the exact layout it was built against: the same bytes at a different address jump
# somewhere else. This is what makes a cheat unportable by byte-matching alone.
#   E8 rel32  call     E9 rel32  jmp      EB rel8  jmp short
#   0F 80..8F rel32    near conditional jumps
#   70..7F rel8        short conditional jumps
# RIP-relative memory operands (modrm mod=00 reg=xxx rm=101) are also position-dependent, and the
# common encodings that show up in these files are caught by the 4C 3B 3D / 48 8B 05 style prefixes.
RIP_PREFIXES = (b"\x4c\x3b\x3d", b"\x48\x8b\x05", b"\x48\x8d\x05", b"\x48\x3b\x05",
                b"\x4c\x8d\x3d", b"\x48\x8b\x0d")


def classify_run(b):
    """Reasons this byte run is tied to the exact address it was built for.

    THIS IS A HEURISTIC AND IT SAYS SO. It does not decode x86 - it looks for the few encodings that
    are unambiguous enough to be worth reporting, and deliberately does NOT guess at short jumps: a
    first version flagged `8B8378010000` (mov eax,[rbx+0x178]) as a conditional jump because 0x78 can
    begin one, which is exactly the kind of confident nonsense that makes a tool untrustworthy.

    What is reported:
      E8 / E9 rel32   a call or jump whose operand is a distance from the instruction itself
      RIP-relative    a memory operand addressed from the instruction pointer
    Both mean the same thing: write these bytes at a different address and they refer somewhere else.
    """
    why = []
    for i in range(len(b)):
        if b[i] in (0xE8, 0xE9) and i + 5 <= len(b):
            why.append("%s rel32 at +%d" % ("call" if b[i] == 0xE8 else "jmp", i))
    for p in RIP_PREFIXES:
        if p in b:
            why.append("RIP-relative operand (%s)" % p.hex())
    return why


# The prefixes the engine itself decodes (ps5-app/onconsole/server.c, RIP_PFX). Same list, same
# order, on purpose: if the two disagree the doctor stops describing the engine.
RIP_PFX = (b"\x4c\x3b\x3d", b"\x48\x8b\x05", b"\x48\x8d\x05", b"\x48\x3b\x05",
           b"\x4c\x8d\x3d", b"\x48\x8b\x0d", b"\x48\x89\x1d", b"\x48\x8b\x1d",
           b"\x48\x89\x05", b"\x48\x8b\x15", b"\x48\x89\x0d", b"\x48\x89\x15")


def rip_reach(run, run_off):
    """Image-relative targets of the RIP-relative operands in a run, as the engine computes them.

    WHY THIS MATTERS MORE THAN ANYTHING ELSE THE DOCTOR SAYS. Dark Souls II's master code does
    `mov [rip-0x2473807], rbx` from image offset +0x2077807, which lands at -0x3FC000 - absolute
    0x4000 on these consoles, not inside any module the game has loaded. The game faults the first
    time that code runs. Five entries in the whole shipped library reach outside the image, all of
    them that same slot, in three files. The engine refuses them; this says why."""
    out = []
    for p in RIP_PFX:
        i = 0
        while True:
            i = run.find(p, i)
            if i < 0:
                break
            if i + 7 <= len(run):
                disp = int.from_bytes(run[i + 3:i + 7], "little", signed=True)
                out.append((p.hex(), run_off + i + 7 + disp))
            i += 1
    return out


def _hx(v):
    """Bytes from a hex string, tolerating whitespace and returning b'' for anything unusable - some
    files in the library carry separators or a stray character, and one bad entry must not stop a
    survey of six thousand of them."""
    t = "".join(str(v or "").split())
    if len(t) % 2:
        t = t[:-1]
    try:
        return bytes.fromhex(t)
    except ValueError:
        return b""


def load_json_cheat(tid, ver=None, prefer=None):
    """The cheat file for a title. `prefer` is the file the CONSOLE said it uses.

    A title can have several files for one version (CUSA02290 has _01.33 and _01.33_2), and a doctor
    that diagnoses a different file than the app applies is worse than no doctor - it would report a
    mismatch nobody can act on. So when the console names its file, that file wins."""
    d = os.path.join(CHEATS, "json")
    if not os.path.isdir(d):
        return None, None
    cands = sorted(n for n in os.listdir(d) if n.startswith(tid + "_") and n.endswith(".json"))
    if not cands:
        return None, None
    pick = None
    if prefer and prefer in cands:
        pick = prefer
    if not pick and ver:
        for n in cands:
            if n == "%s_%s.json" % (tid, ver):
                pick = n
                break
    pick = pick or cands[-1]
    return pick, json.load(io.open(os.path.join(d, pick), encoding="utf-8"))


def api(path, timeout=60):
    with urllib.request.urlopen(PC + path, timeout=timeout) as f:
        return json.loads(f.read().decode())


def console_url(platform):
    """The LAN address of a console, ASKED OF THE COMPANION - never a constant in this file.

    Two consoles move between two houses in this project and a hardcoded address in a tool has
    already cost a debugging session, so the address comes from /api/devices, which is where the app
    itself keeps it."""
    try:
        d = api("/api/devices", timeout=10)
    except Exception:
        return None
    for c in (d.get("consoles") or []):
        if str(c.get("platform") or "").lower() == platform and c.get("ip"):
            return "http://%s:%d" % (c["ip"], int(c.get("shop_port") or 8710))
    return None


def running(tid):
    """(platform, pid, base, console_url) for a running title, or None.

    /api/mem/read lives ON THE CONSOLE - the companion does not carry it, and nothing here should
    invent a route that does not exist (an earlier version of this tool asked the PC for it and
    reported every entry "unreadable", which reads like a broken engine and was a broken tool)."""
    for con in ("ps4", "ps5"):
        try:
            d = api("/api/mods/%s?console=%s&state=1" % (tid, con), timeout=60)
        except Exception:
            continue
        if d.get("running") and d.get("pid") and d.get("base"):
            u = console_url(con)
            if not u:
                continue
            return con, int(d["pid"]), int(str(d["base"]), 16), u, str(d.get("file") or "")
    return None


def read_mem(live, addr, n):
    """n bytes from the running game, or None. The route clamps to 256 per call, so ask in chunks -
    asking for more and believing the short answer is how a check stops checking."""
    pid, url = live[1], live[3]
    out = b""
    while len(out) < n:
        want = min(256, n - len(out))
        try:
            with urllib.request.urlopen(
                    "%s/api/mem/read?pid=%d&addr=0x%X&len=%d" % (url, pid, addr + len(out), want),
                    timeout=40) as f:
                d = json.loads(f.read().decode())
        except Exception:
            return None
        if d.get("rc") != 0 or not d.get("hex"):
            return None
        got = bytes.fromhex(d["hex"])
        if not got:
            return None
        out += got
    return out[:n]


def report_one(name, mods, live, unreach=None):
    hard = ready = already = wrong = unknown = 0
    if unreach is None:
        unreach = [0]
    print("\n  %s" % name)
    for m in mods:
        mem = m.get("memory") or []
        runs = []
        for e in mem:
            on = _hx(e.get("on"))
            off = _hx(e.get("off"))
            try:
                runs.append((int(str(e.get("offset")), 16), on, off))
            except ValueError:
                continue
        reasons = []
        cave = False
        for _, on, off in runs:
            reasons += classify_run(on) + classify_run(off)
            if off and set(off) == {0}:
                cave = True
        kind = "CODE CAVE + JUMP" if cave else ("position-dependent" if reasons else "plain byte patch")
        if cave or reasons:
            hard += 1
        verdicts = []
        if live:
            base = live[2]
            for off_, on, offb in runs:
                want = max(len(on), len(offb))
                cur = read_mem(live, base + off_, want)
                if cur is None:
                    verdicts.append("unreadable")
                    unknown += 1
                elif offb and cur[:len(offb)] == offb:
                    verdicts.append("OFF (ready)")
                    ready += 1
                elif on and cur[:len(on)] == on:
                    verdicts.append("ON (applied)")
                    already += 1
                else:
                    verdicts.append("NEITHER: has %s" % cur[:max(len(on), len(offb))].hex().upper()[:24])
                    wrong += 1
        print("    %-28s %-18s entries=%d%s"
              % ((m.get("name") or "?")[:28], kind, len(runs),
                 ("  -> " + ", ".join(verdicts)) if verdicts else ""))
        if reasons:
            print("        position-dependent: %s" % "; ".join(sorted(set(reasons))[:4]))
        # WHERE THIS CHEAT'S OWN CODE POINTS. A target below the image cannot be inside the
        # executable; one inside it is settled by reading a byte when the game is running.
        for off_, on, offb in runs:
            for pfx, tgt in rip_reach(on, off_):
                if tgt < 0:
                    print("        UNREACHABLE: %s reaches image offset %+#x, below the"
                          % (pfx, tgt))
                    print("                     executable. The engine refuses this cheat: it")
                    print("                     needs scratch memory this console does not map.")
                    unreach[0] += 1
                elif live and read_mem(live, live[2] + tgt, 1) is None:
                    print("        UNREACHABLE: %s reaches image offset %+#x, which cannot be read"
                          % (pfx, tgt))
                    unreach[0] += 1
    return hard, ready, already, wrong, unknown


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("title_id", nargs="?")
    ap.add_argument("--version")
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()

    if a.all:
        d = os.path.join(CHEATS, "json")
        tot = cave = dep = plain = 0
        for n in sorted(os.listdir(d)):
            if not n.endswith(".json"):
                continue
            try:
                j = json.load(io.open(os.path.join(d, n), encoding="utf-8"))
            except Exception:
                continue
            for m in (j.get("mods") or []):
                tot += 1
                runs = [(_hx(e.get("on")), _hx(e.get("off")))
                        for e in (m.get("memory") or [])]
                if any(off and set(off) == {0} for _, off in runs):
                    cave += 1
                elif any(classify_run(on) or classify_run(off) for on, off in runs):
                    dep += 1
                else:
                    plain += 1
        print("the whole JSON library, by what each mod DOES:")
        print("  %6d mods total" % tot)
        print("  %6d plain byte patches      - portable by finding the same bytes again" % plain)
        print("  %6d position-dependent      - contain a relative jump/call or RIP operand" % dep)
        print("  %6d CODE CAVE + JUMP        - NOT portable by byte matching; the cave address and"
              % cave)
        print("         every displacement in it belong to one executable")
        return 0

    if not a.title_id:
        print("give a title id, or --all")
        return 2
    live = running(a.title_id)
    # An explicit --version is the caller's own question and outranks the console's choice.
    fn, j = load_json_cheat(a.title_id, a.version,
                            prefer=(live[4] if (live and not a.version) else None))
    if not j:
        print("no JSON cheat file for %s" % a.title_id)
        return 1
    print("file: %s   (version %r, process %r)" % (fn, j.get("version"), j.get("process")))
    if live:
        print("live: %s pid=%d base=0x%X  - every run below was READ from the running game"
              % (live[0].upper(), live[1], live[2]))
    else:
        print("live: the game is not running - classification only, nothing was read")
    unreach = [0]
    # THE MASTER CODE, when the file has one. The engine installs it before the first cheat, so
    # its own reachability decides whether any cave-resident cheat in the file can work at all.
    mas = j.get("master") or {}
    if mas.get("memory"):
        print("")
        print("  master code: %d entr%s, installed before the first cheat"
              % (len(mas["memory"]), "y" if len(mas["memory"]) == 1 else "ies"))
        # A master entry is not judged the way a mod is, so its OFF/ON/NEITHER column would mislead:
        # the engine requires a cave to be EMPTY and a hook to hold code the cave re-executes, neither
        # of which is an "off state". Said here rather than left to be inferred.
        print("  (a cave must be empty and a hook must hold code the cave re-executes - so NEITHER")
        print("   against a master entry is not by itself a fault; the UNREACHABLE lines are)")
        report_one("  its entries", [{"name": "the master", "memory": mas["memory"]}],
                   live, unreach)
    hard, ready, already, wrong, unknown = report_one(j.get("name") or a.title_id,
                                                      j.get("mods") or [], live, unreach)
    if unreach[0]:
        print("")
        print("  %d entr%s here reach memory the game does not have. The engine refuses those:"
              % (unreach[0], "y" if unreach[0] == 1 else "ies"))
        print("  applying one crashes the game the first time the patched code runs, which is")
        print("  exactly what happened here before the check existed.")
    if live:
        print("\n  entries: %d ready, %d already applied, %d DO NOT MATCH this build, %d unreadable"
              % (ready, already, wrong, unknown))
        if wrong:
            print("  -> an entry that matches NEITHER state was not built for this executable.")
            print("     The engine refuses those (it gates every write on the documented bytes), so")
            print("     the mod is declined rather than applied halfway.")
    if hard:
        print("")
        print("  %d mod(s) here are position-dependent or install a code cave." % hard)
        print("  Those cannot be ported to another build by matching bytes: their jumps carry")
        print("  displacements computed for THIS executable, and the same bytes written at a")
        print("  different address refer somewhere else entirely.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Move a cheat from one build of a game to another, and refuse when that cannot be done honestly.

THE QUESTION. A title usually has several cheat files, one per game version, and they are not equal:
Dark Souls II ships 1 mod for 01.00 and 7 for 01.02. An owner on 01.00 can see the seven but cannot
use them, because every offset in that file belongs to a different executable. The same happens
across regions, where the same game has several title ids.

HOW THIS IS POSSIBLE AT ALL. A cheat is bytes at an offset. If the SAME cheat exists in both files -
same number of entries, byte-for-byte identical `on` and `off` runs - then the difference between its
two offsets is how far that code moved between the builds. Those shared cheats are anchors, and a
cheat that exists in only one file can be placed by applying the anchors' delta.

MEASURED, over the library we ship: 87 titles have two or more comparable versions, and for 70 of
them ONE constant delta explains every anchor. That is what makes this worth doing rather than
guessing. The remaining 17 disagree, and for those this refuses instead of picking a favourite.

WHAT IT WILL NOT DO, and this is most of the value:
  * a code cave is never ported. 8,128 of the 15,019 mods in the library install a routine into empty
    space and jump into it; the jumps carry displacements computed for one layout, and the same bytes
    at a different address refer somewhere else. Byte matching cannot move that. See
    internal/research/cheat-formats.md.
  * an anchor set that does not agree on a delta produces nothing.
  * a delta of exactly +/-0x400000 is reported as what it is - one file written with absolute
    addresses and the other image-relative - and not as code having moved.
  * nothing is ever written into a game here. The output is a cheat file, and the engine's own gate
    still refuses any entry whose target does not already hold the documented bytes.

    python tools/cheat_port.py --title CUSA01589 --from 01.02 --to 01.00
    python tools/cheat_port.py --title CUSA01589 --from 01.02 --to 01.00 --write
    python tools/cheat_port.py --region CUSA00207 CUSA00208
    python tools/cheat_port.py --title CUSA02290 --from 01.33 --to 01.33 --verify

--verify reads the RUNNING game through the console and checks that each ported entry's `off` bytes
are really there. That is the only thing that turns an arithmetic estimate into a fact, and it is
what tools/cheat_doctor.py does for a whole file.
"""
import argparse
import collections
import io
import json
import os
import re
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
CHEATS = os.path.join(ROOT, "assets", "cheats", "json")
PC = "http://127.0.0.1:8710"

IMAGE_BASE = 0x400000        # PS4/PS5 no-ASLR load address; a delta of this is a notation change

RIP_PREFIXES = (b"\x4c\x3b\x3d", b"\x48\x8b\x05", b"\x48\x8d\x05", b"\x48\x3b\x05",
                b"\x4c\x8d\x3d", b"\x48\x8b\x0d")


def hx(v):
    t = "".join(str(v or "").split())
    if len(t) % 2:
        t = t[:-1]
    try:
        return bytes.fromhex(t)
    except ValueError:
        return b""


def norm(s):
    """A cheat name reduced to what two files are likely to agree on. Authors re-type these by hand:
    'lnfinite Stamina' (lowercase L for I), trailing spaces, 'Inf.' for 'Infinite'."""
    t = (s or "").lower().replace("inf.", "infinite").replace("infinit ", "infinite ")
    return re.sub(r"[^a-z0-9]", "", t)


def entries(mod):
    out = []
    for e in (mod.get("memory") or []):
        try:
            off = int(str(e.get("offset")), 16)
        except (TypeError, ValueError):
            continue
        out.append({"off": off, "on": hx(e.get("on")), "offb": hx(e.get("off")),
                    "section": str(e.get("section") or ""), "raw": e})
    return out


def is_cave(ents):
    return any(e["offb"] and set(e["offb"]) == {0} for e in ents)


def master_span(doc):
    """The offset ranges the master block writes into, as [(lo, hi)].

    Needed because of what Dark Souls II turned out to be: five of its seven cheats are byte patches
    INSIDE the routine the master installs (their `off` bytes are the master's own bytes at that
    offset, verified byte for byte). Those look like ordinary portable byte patches - real original
    bytes, no zeros, no relative jumps - and they are not portable at all, because the address they
    sit at is the address of a cave chosen for one build. Porting one would write a correct-looking
    patch into whatever happens to live there in the other build."""
    m = doc.get("master") or {}
    out = []
    for e in (m.get("memory") or []):
        try:
            lo = int(str(e.get("offset")), 16)
        except (TypeError, ValueError):
            continue
        n = max(len(hx(e.get("on"))), len(hx(e.get("off"))))
        if n:
            out.append((lo, lo + n))
    return out


def position_dependent(ents):
    """Reasons this mod is tied to one layout. Same heuristic as cheat_doctor, deliberately narrow:
    E8/E9 rel32 and the RIP-relative prefixes only, never short jumps."""
    why = []
    for e in ents:
        for b in (e["on"], e["offb"]):
            for i in range(len(b)):
                if b[i] in (0xE8, 0xE9) and i + 5 <= len(b):
                    why.append("%s rel32" % ("call" if b[i] == 0xE8 else "jmp"))
            for p in RIP_PREFIXES:
                if p in b:
                    why.append("RIP-relative (%s)" % p.hex())
    return sorted(set(why))


def load(tid, ver):
    """The file for a title at a version, plus its name. Accepts an exact version or a file name."""
    if not os.path.isdir(CHEATS):
        return None, None
    names = sorted(n for n in os.listdir(CHEATS) if n.startswith(tid + "_") and n.endswith(".json"))
    if not names:
        return None, None
    pick = None
    for n in names:
        if n == ver or n == "%s_%s.json" % (tid, ver):
            pick = n
            break
    if not pick:
        # a version with suffixed variants (CUSA02290_01.33_2.json): take the plain one first
        for n in names:
            if n.startswith("%s_%s" % (tid, ver)):
                pick = n
                break
    if not pick:
        return None, None
    return pick, json.load(io.open(os.path.join(CHEATS, pick), encoding="utf-8"))


def anchors(A, B):
    """Cheats present in BOTH files with identical byte runs. Returns [(name, delta, n_entries)].

    Identical runs are the whole point: a cheat whose bytes differ is a different patch, and using it
    as an anchor would measure the difference between two unrelated things."""
    ma = {norm(m.get("name")): m for m in (A.get("mods") or [])}
    mb = {norm(m.get("name")): m for m in (B.get("mods") or [])}
    out = []
    for k, a in ma.items():
        b = mb.get(k)
        if not b:
            continue
        ea, eb = entries(a), entries(b)
        if len(ea) != len(eb) or not ea:
            continue
        if any(x["on"] != y["on"] or x["offb"] != y["offb"] for x, y in zip(ea, eb)):
            continue
        deltas = {y["off"] - x["off"] for x, y in zip(ea, eb)}
        if len(deltas) != 1:
            continue                      # the entries of one cheat disagree: not an anchor
        out.append((a.get("name") or k, deltas.pop(), len(ea)))
    return out


def sig_find(live, pattern, frm=0, to=0, budget=600):
    """Where does this byte pattern appear in the running module? Image offsets, or None.

    THE PRIMITIVE THAT MAKES PORTING WITHOUT ANCHORS POSSIBLE. Before it existed, a cheat could only
    be moved between builds by measuring the distance with cheats present in BOTH files - which works
    for 70 of 87 comparable title pairs and for nothing else. With it, a plain byte patch can be moved
    by finding its documented original bytes in the target.

    Measured on a PS4: a full 34 MB module takes about 230 s (the agent reads 4 KB per request at a
    25 ms poll) and /api/mods, which shares that channel, slows to about 3.4 s while it runs. So this
    is a deliberate operation with a progress poll, never something to do per entry without saying so.
    A PS5 reads memory directly and is seconds."""
    _, pid, base, url = live
    q = "%s/api/mem/find?pattern=%s&pid=%d&base=0x%X" % (url, pattern, pid, base)
    if frm or to:
        q += "&from=%d&to=%d" % (frm, to)
    try:
        with urllib.request.urlopen(q, timeout=40) as f:
            r = json.loads(f.read().decode())
    except Exception as e:
        print("   the search could not be started: %s" % str(e)[:80])
        return None
    if not r.get("ok"):
        print("   the search was refused: %s" % (r.get("error") or r.get("rc")))
        return None
    t0 = time.time()
    last = -1
    while time.time() - t0 < budget:
        try:
            with urllib.request.urlopen("%s/api/mem/find/status" % url, timeout=30) as f:
                st = json.loads(f.read().decode())
        except Exception:
            time.sleep(2)
            continue
        p = st.get("percent", 0)
        if p >= last + 20:
            last = p - (p % 20)
            print("   searching... %d%%  %d found" % (p, st.get("found", 0)))
        if not st.get("active"):
            if st.get("truncated"):
                print("   the search hit its match ceiling - the pattern is not distinctive")
                return None
            # A SWEEP THAT COULD NOT READ IS NOT A SWEEP THAT FOUND NOTHING. The status carries how
            # many chunks were skipped; treating that as "those bytes are not in this build" is a
            # confident wrong answer about somebody's cheat file.
            hits = [int(x, 16) for x in (st.get("offsets") or [])]
            if not hits and st.get("gaps"):
                print("   %d chunk(s) could not be read, so this did not search the whole range"
                      % st.get("gaps"))
                return None
            return hits
        time.sleep(3)
    print("   the search did not finish inside %ds - cancelling" % budget)
    try:
        urllib.request.urlopen("%s/api/mem/find/cancel" % url, timeout=20).read()
    except Exception:
        pass
    return None


def api(path, timeout=40):
    with urllib.request.urlopen(PC + path, timeout=timeout) as f:
        return json.loads(f.read().decode())


def live_for(tid):
    """(platform, pid, base, console_url) for a running title, or None. The address comes from the
    companion's /api/devices - never a constant in this file."""
    for con in ("ps4", "ps5"):
        try:
            d = api("/api/mods/%s?console=%s&state=1" % (tid, con), timeout=60)
        except Exception:
            continue
        if d.get("running") and d.get("pid") and d.get("base"):
            try:
                dv = api("/api/devices", timeout=10)
            except Exception:
                return None
            for c in (dv.get("consoles") or []):
                if str(c.get("platform") or "").lower() == con and c.get("ip"):
                    return (con, int(d["pid"]), int(str(d["base"]), 16),
                            "http://%s:%d" % (c["ip"], int(c.get("shop_port") or 8710)))
    return None


def read_mem(live, addr, n):
    """n bytes out of the running game. The console route clamps to 256 per call, so ask in chunks -
    asking for more and believing the short answer is how a check stops checking."""
    _, pid, _, url = live
    got = b""
    while len(got) < n:
        want = min(256, n - len(got))
        try:
            with urllib.request.urlopen(
                    "%s/api/mem/read?pid=%d&addr=0x%X&len=%d" % (url, pid, addr + len(got), want),
                    timeout=40) as f:
                d = json.loads(f.read().decode())
        except Exception:
            return None
        if d.get("rc") != 0 or not d.get("hex"):
            return None
        chunk = bytes.fromhex(d["hex"])
        if not chunk:
            return None
        got += chunk
    return got[:n]


def describe_delta(d):
    if d == 0:
        return "0 (the same addresses)"
    if abs(d) == IMAGE_BASE:
        return ("%+#x - NOT a code move: one file is written with absolute addresses and the other "
                "image-relative" % d)
    return "%+#x" % d


def by_signature(a):
    """Port to the running game by searching for each entry's documented original bytes.

    No anchors, no second file, no arithmetic - the address comes from the game itself. What it costs
    is time: one full sweep of the module per entry, about 230 s on a PS4. What it demands is
    uniqueness, because the same six bytes really do appear six times in a 34 MB module."""
    if not (a.title and a.src):
        print("--by-signature needs --title and --from (the file whose cheats you want to move)")
        return 2
    fa, A = load(a.title, a.src)
    if not A:
        print("no cheat file for %s at %s" % (a.title, a.src))
        return 1
    print("from: %s   (%d mods)" % (fa, len(A.get("mods") or [])))

    msp = master_span(A)
    todo, skip = [], []
    for m in (A.get("mods") or []):
        ents = entries(m)
        if not ents:
            skip.append((m.get("name"), "no usable entries"))
        elif is_cave(ents):
            skip.append((m.get("name"), "installs a code cave - not portable by matching bytes"))
        elif position_dependent(ents):
            skip.append((m.get("name"), "position-dependent: %s"
                         % ", ".join(position_dependent(ents)[:2])))
        elif any(lo <= e["off"] < hi for e in ents for lo, hi in msp):
            skip.append((m.get("name"), "patches the master code's own routine"))
        elif any(e["section"] and e["section"] not in ("0", "") for e in ents):
            skip.append((m.get("name"), "targets another loaded module (section) - the engine refuses "
                         "those outright, so placing it would produce an address it declines"))
        elif any(len(e["offb"]) < 4 for e in ents):
            skip.append((m.get("name"), "its original bytes are too short to search for (under 4)"))
        else:
            todo.append((m, ents))

    for name, why in skip:
        print("   skipped   %-34s %s" % ((name or "?")[:34], why))
    n_searches = sum(len(e) for _, e in todo)
    if not n_searches:
        print("\nnothing here can be ported by searching.")
        return 0

    # THE CONSOLE IS ONLY NEEDED FROM HERE. What a file CAN offer is a property of the file, and an
    # owner should be able to ask that without the game open - which is also what makes the refusal
    # ladder above testable without a console.
    live = live_for(a.title)
    if not live:
        print("\n%d mod(s) could be placed by searching, but %s is not running - and this reads the"
              % (len(todo), a.title))
        print("game itself. Start it and run this again.")
        return 1
    print("into: the RUNNING %s  pid=%d base=0x%X" % (live[0].upper(), live[1], live[2]))
    print("\n%d mod(s) to place, %d search(es). Each one sweeps the whole module - on a PS4 that is"
          % (len(todo), n_searches))
    print("about 230 s per search, so this will take roughly %d minute(s)." % max(1, n_searches * 4))

    ported, refused = [], []
    for m, ents in todo:
        name = m.get("name") or "?"
        new = json.loads(json.dumps(m))
        ok = True
        for e, src in zip(new.get("memory") or [], ents):
            pat = src["offb"].hex().upper()
            print("\n  %s  <- searching for %s" % (name[:40], pat))
            hits = sig_find(live, pat)
            if hits is None:
                refused.append((name, "the search did not complete"))
                ok = False
                break
            if len(hits) == 0:
                refused.append((name, "those bytes are not in this build"))
                ok = False
                break
            if len(hits) > 1:
                refused.append((name, "those bytes appear %d times - not distinctive enough to place"
                                % len(hits)))
                print("   found at: %s" % ", ".join("%X" % h for h in hits[:8]))
                ok = False
                break
            e["offset"] = "%X" % hits[0]
            print("   exactly one match: %X  (was %X)" % (hits[0], src["off"]))
        if ok:
            ported.append(new)

    print("")
    for m in ported:
        print("   PLACED    %-34s -> %s" % ((m.get("name") or "?")[:34],
                                            ", ".join(e["offset"] for e in (m.get("memory") or []))))
    for name, why in refused:
        print("   refused   %-34s %s" % ((name or "?")[:34], why))
    if not ported:
        print("\nnothing could be placed.")
        return 1

    out = json.loads(json.dumps(A))
    out["mods"] = ported
    note = "placed %d mod(s) by searching the running game" % len(ported)
    cr = out.get("credits")
    out["credits"] = (cr + [note]) if isinstance(cr, list) else ((cr or "") + "  |  " + note)
    dest = a.out or (os.path.join(CHEATS, "%s_found.json" % a.title) if a.write else None)
    if not dest:
        print("\n%s" % note)
        print("nothing was written. Pass --out PATH, or --write to put it beside the library as")
        print("%s_found.json. The engine's own gate still decides whether to apply it." % a.title)
        return 0
    io.open(dest, "w", encoding="utf-8", newline="\n").write(
        json.dumps(out, indent=2, ensure_ascii=False))
    print("\nwrote %s  (%d mods, %s)" % (dest, len(ported), note))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--title")
    ap.add_argument("--from", dest="src")
    ap.add_argument("--to", dest="dst")
    ap.add_argument("--region", nargs=2, metavar=("TID_A", "TID_B"))
    ap.add_argument("--write", action="store_true",
                    help="write the ported file into assets/cheats/json")
    ap.add_argument("--out", help="write it here instead")
    ap.add_argument("--force", action="store_true",
                    help="write even when a single anchor is all there is to go on")
    ap.add_argument("--verify", action="store_true",
                    help="read the running game and keep only entries whose off bytes are really there")
    ap.add_argument("--by-signature", dest="bysig", action="store_true",
                    help="find each cheat's bytes in the RUNNING game instead of using anchors")
    a = ap.parse_args()

    if a.bysig:
        return by_signature(a)

    if a.region:
        ta, tb = a.region
        fa, A = load(ta, "")
        fb, B = load(tb, "")
        if not A or not B:
            print("need a cheat file for both %s and %s" % (ta, tb))
            return 1
        dst_tid = tb
    else:
        if not (a.title and a.src and a.dst):
            print("give --title with --from and --to, or --region A B")
            return 2
        fa, A = load(a.title, a.src)
        fb, B = load(a.title, a.dst)
        if not A:
            print("no cheat file for %s at %s" % (a.title, a.src))
            return 1
        if not B:
            print("no cheat file for %s at %s - nothing to port ONTO" % (a.title, a.dst))
            return 1
        dst_tid = a.title

    print("from: %s   (%d mods)" % (fa, len(A.get("mods") or [])))
    print("onto: %s   (%d mods)" % (fb, len(B.get("mods") or [])))

    anch = anchors(A, B)
    if not anch:
        print("\nNO ANCHORS. Not one cheat appears in both files with identical bytes, so there is")
        print("nothing to measure the difference between these builds with. Porting would be a guess.")
        return 1

    dist = collections.Counter(d for _, d, _ in anch)
    print("\nanchors (the same cheat in both files, byte for byte):")
    for name, d, n in sorted(anch, key=lambda x: x[0].lower())[:12]:
        print("   %-34s %-3d entr%s  delta %s" % (name[:34], n, "y" if n == 1 else "ies",
                                                 describe_delta(d)))
    if len(anch) > 12:
        print("   ... and %d more" % (len(anch) - 12))

    delta, agree = dist.most_common(1)[0]
    print("\n%d anchor(s), %d distinct delta(s); the commonest is %s (%d of %d agree)"
          % (len(anch), len(dist), describe_delta(delta), agree, len(anch)))
    if len(dist) > 1:
        print("\nTHE ANCHORS DISAGREE, so there is no single answer to \"how far did the code move\".")
        print("The other deltas seen: %s" % ", ".join(describe_delta(d) for d in list(dist)[1:6]))
        print("Nothing is ported from a disagreement - the offsets it produced would be somewhere")
        print("between two builds and belong to neither.")
        return 1

    # ---- what is missing, and can it be placed? ------------------------------------------------
    have = {norm(m.get("name")) for m in (B.get("mods") or [])}
    msp = master_span(A)                 # see master_span: cave-resident cheats are not portable
    ported, refused = [], []
    for m in (A.get("mods") or []):
        k = norm(m.get("name"))
        if k in have:
            continue
        ents = entries(m)
        if not ents:
            refused.append((m.get("name"), "no usable entries"))
            continue
        if is_cave(ents):
            refused.append((m.get("name"),
                            "installs a code cave - its jumps belong to one executable"))
            continue
        pd = position_dependent(ents)
        if pd:
            refused.append((m.get("name"), "position-dependent: %s" % ", ".join(pd[:3])))
            continue
        if any(e["section"] and e["section"] not in ("0", "") for e in ents):
            refused.append((m.get("name"), "targets another loaded module (section)"))
            continue
        if any(lo <= e["off"] < hi for e in ents for lo, hi in msp):
            refused.append((m.get("name"),
                            "patches the master code's own routine - that cave belongs to this build"))
            continue
        new = json.loads(json.dumps(m))          # a copy, so the source file is never touched
        for e, src in zip(new.get("memory") or [], ents):
            e["offset"] = "%X" % (src["off"] + delta)
        ported.append(new)

    print("\n%d mod(s) in %s are not in %s:" % (len(ported) + len(refused), fa, fb))
    for m in ported:
        print("   PORTABLE  %-34s -> %s" % ((m.get("name") or "?")[:34],
                                            ", ".join(e["offset"] for e in (m.get("memory") or []))))
    for name, why in refused:
        print("   refused   %-34s %s" % ((name or "?")[:34], why))
    if not ported:
        print("\nnothing to port.")
        return 0

    # ---- verify against the running game -------------------------------------------------------
    if a.verify:
        live = live_for(dst_tid)
        if not live:
            print("\n--verify asked for, but %s is not running - nothing was checked, so nothing is"
                  " kept. Start the game and run it again." % dst_tid)
            return 1
        print("\nverifying against %s pid=%d base=0x%X" % (live[0].upper(), live[1], live[2]))
        keep = []
        for m in ported:
            ok = True
            for e in (m.get("memory") or []):
                offb = hx(e.get("off"))
                on = hx(e.get("on"))
                want = max(len(offb), len(on)) or 1
                cur = read_mem(live, live[2] + int(e["offset"], 16), want)
                if cur is None:
                    print("   ?  %-32s could not read %s" % ((m.get("name") or "")[:32], e["offset"]))
                    ok = False
                elif offb and cur[:len(offb)] == offb:
                    pass                                   # exactly the documented original code
                elif on and cur[:len(on)] == on:
                    print("   ok %-32s %s is ALREADY the on-state" % ((m.get("name") or "")[:32],
                                                                     e["offset"]))
                else:
                    print("   no %-32s %s holds %s" % ((m.get("name") or "")[:32], e["offset"],
                                                       cur[:8].hex().upper()))
                    ok = False
            if ok:
                print("   OK %-32s every entry is where the file says" % (m.get("name") or "")[:32])
                keep.append(m)
        print("\n%d of %d ported mod(s) verified in the running game" % (len(keep), len(ported)))
        ported = keep
        if not ported:
            return 1

    # ---- the file ------------------------------------------------------------------------------
    out = json.loads(json.dumps(B))
    out["mods"] = (out.get("mods") or []) + ported
    note = "ported %d mod(s) from %s with delta %+#x" % (len(ported), fa, delta)
    # `credits` is a STRING in most files and a LIST in some - concatenating blindly threw. Whatever
    # shape it has, the note is added without destroying what was there.
    cr = out.get("credits")
    if isinstance(cr, list):
        out["credits"] = cr + [note]
    else:
        out["credits"] = ((cr or "") + ("  |  " if cr else "") + note)
    # ONE ANCHOR IS ONE MEASUREMENT. It is enough to make an estimate and not enough to trust
    # blindly: a single cheat that happens to appear in both files, with one entry, is exactly the
    # case where a file could have been assembled from two sources. So writing off one anchor needs
    # either a verification against the running game or a deliberate --force.
    thin = len(anch) < 2 and delta != 0
    if thin and (a.write or a.out) and not (a.verify or a.force):
        print("")
        print("ONE ANCHOR ONLY. That is a single measurement of how far the code moved, and these")
        print("offsets rest entirely on it. Run it again with --verify while the game is running, or")
        print("with --force if you mean it. Nothing was written.")
        return 1
    if a.out:
        dest = a.out
    elif a.write:
        dest = os.path.join(CHEATS, fb)
    else:
        print("\n%s" % note)
        print("nothing was written. Pass --write to update %s, or --out PATH." % fb)
        print("Verify first (--verify) if the game is running: the engine will refuse a wrong entry,")
        print("but a refusal is a worse answer than not offering the cheat at all.")
        return 0
    io.open(dest, "w", encoding="utf-8", newline="\n").write(
        json.dumps(out, indent=2, ensure_ascii=False))
    print("\nwrote %s  (%d mods now, %s)" % (dest, len(out["mods"]), note))
    return 0


if __name__ == "__main__":
    sys.exit(main())

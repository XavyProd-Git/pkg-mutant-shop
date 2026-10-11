# -*- coding: utf-8 -*-
"""The whole PS4 cheat chain in one screen, link by link, so a failure names itself.

There are six things between "a game is running" and "a cheat is on", they fail for different
reasons, and from the panel they all look the same: a tile that will not turn green. This walks
them in order and stops at the first one that is not true, saying what to do about it.

    python tools/ps4_cheat_status.py               # find the PS4 through the companion
    python tools/ps4_cheat_status.py --ip 10.0.0.86
    python tools/ps4_cheat_status.py --apply       # ALSO turn the first mod on, and read it back

--apply writes into the running game. It is not the default for that reason, it refuses unless
every link before it is green, and it reads the bytes back afterwards rather than trusting the
reply - the engine's own expect-gate means a write that was refused reports failure, but a write
that landed somewhere unexpected would not.
"""
import argparse
import json
import sys
import urllib.parse
import urllib.request

OK = "  [ok]   "
NO = "  [--]   "


def get(url, timeout=30):
    req = urllib.request.Request(url)
    # Every changing route on the console carries the cross-site guard, and these headers are what
    # a page served by the console itself sends. Reading routes ignore them.
    req.add_header("Origin", url.split("/api/")[0])
    req.add_header("Sec-Fetch-Site", "same-origin")
    req.add_header("Sec-Fetch-Mode", "cors")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ip", default="")
    ap.add_argument("--companion", default="http://127.0.0.1:8710")
    ap.add_argument("--apply", action="store_true")
    a = ap.parse_args()

    ip = a.ip
    if not ip:
        try:
            for c in (get(a.companion + "/api/consoles").get("consoles") or []):
                if str(c.get("platform") or "").lower() == "ps4":
                    ip = c.get("ip")
        except Exception as e:
            print("could not ask the companion which console is the PS4: %s" % e)
    if not ip:
        print("no PS4 address - pass --ip")
        return 2
    base = "http://%s:8710" % ip
    print("PS4 %s\n" % ip)

    # 1 - the shop
    try:
        h = get(base + "/api/health", timeout=15)
    except Exception as e:
        print(NO + "the shop is not answering: %s" % e)
        print("        Press the PKG MUTANT SHOP icon on the console, or start the PC app and "
              "wait a few minutes - it reloads the payload by itself.")
        return 1
    print(OK + "the shop answers        v%s  built %s" % (h.get("version"), h.get("built")))

    # 2 - the helper is installed and listed
    ag = get(base + "/api/engine/agent")
    if not ag.get("installed"):
        print(NO + "the in-game helper is not on the console")
        print("        It ships inside the payload and is written at boot - reload the payload.")
        return 1
    if not ag.get("enabled"):
        print(NO + "the in-game helper is switched off")
        print("        Settings > PS4 in-game helper, or:")
        print("        curl -X POST -d '{\"enabled\":1}' %s/api/engine/agent" % base)
        return 1
    print(OK + "the helper is installed and listed in GoldHEN's plugin list")

    # 3 - a game is running
    run = get(base + "/api/cheat/running")
    if not run.get("running"):
        print(NO + "no game is running")
        print("        Start a game on the console. The helper loads WITH the game, so a game "
              "that was already running before the helper was listed will not work - close it "
              "and open it again.")
        return 1
    tid = run.get("title_id")
    print(OK + "a game is running       %s" % tid)

    # 4 - the helper is INSIDE that game
    if not run.get("helper"):
        print(NO + "the helper is not inside that game")
        print("        It loads with the game. Close this game and open it again.")
        return 1
    if not run.get("can_cheat") or not run.get("pid"):
        print(NO + "the helper answered but reported no image base")
        print("        It could not find eboot.bin in the process. klog (port 3232) has the "
              "[PMS-AGENT] lines.")
        return 1
    print(OK + "the helper is in the game   pid %s  base %s" % (run.get("pid"), run.get("base")))

    # 5 - a cheat file for it
    if not run.get("cheat_file"):
        print(NO + "there is no cheat file on the console for %s" % tid)
        print("        Settings > Cheats & mods > Rescan, or drop a file into the inbox over FTP.")
        return 1
    print(OK + "a cheat file matches    %s  (%s)"
          % (str(run.get("cheat_file")).rsplit("/", 1)[-1], run.get("match")))
    if not run.get("exact"):
        print("         note: that file was written for a different version of this game. The "
              "engine still refuses any write whose target does not already hold the bytes the "
              "file documents, so a mismatch fails closed rather than corrupting the game.")

    # 6 - the mods, and whether their live state can be read
    doc = get(base + "/api/cheat/list?file=" + urllib.parse.quote(run["cheat_file"])
              + "&pid=%s&base=%s" % (run["pid"], run.get("base") or "0x400000"), timeout=60)
    mods = doc.get("mods") or []
    if not mods:
        print(NO + "that cheat file has no mods in it")
        return 1
    readable = [m for m in mods if m.get("state") in ("on", "off", "partial")]
    print(OK + "%d mod(s) listed, %d with a live state read out of the game"
          % (len(mods), len(readable)))
    for m in mods[:10]:
        print("           [%2s] %-40s %-8s entries=%s"
              % (m.get("index"), str(m.get("name"))[:40], m.get("state"), m.get("entries")))
    if not readable:
        print("\n  Every mod reads 'unknown', which means the engine read the game's memory and "
              "found neither the on bytes nor the off bytes - this cheat file is for a different "
              "build of this game. Nothing will be written.")
        return 1

    if not a.apply:
        print("\n  Everything up to the write is green. Add --apply to turn the first readable "
              "mod on and read it back.")
        return 0

    m = readable[0]
    want = 0 if m.get("state") == "on" else 1
    print("\n  applying: %s -> %s" % (m.get("name"), "ON" if want else "OFF"))
    res = get(base + "/api/cheat/apply?file=" + urllib.parse.quote(run["cheat_file"])
              + "&mod=%d&on=%d&pid=%s&base=%s&name=%s"
              % (m.get("index"), want, run["pid"], run.get("base") or "0x400000",
                 urllib.parse.quote(str(m.get("name") or "")[:60])), timeout=60)
    print("  reply   : %s" % json.dumps(res)[:300])

    # Read it back rather than believing the reply.
    doc2 = get(base + "/api/cheat/list?file=" + urllib.parse.quote(run["cheat_file"])
               + "&pid=%s&base=%s" % (run["pid"], run.get("base") or "0x400000"), timeout=60)
    after = [x for x in (doc2.get("mods") or []) if x.get("index") == m.get("index")]
    state = after[0].get("state") if after else "?"
    want_state = "on" if want else "off"
    print("  read back: state is %r (wanted %r)" % (state, want_state))
    if state == want_state:
        print("\n  THE CHEAT IS IN THE GAME. The whole chain works.")
        return 0
    print("\n  The write did not take. `detail` in the reply above says how many entries were "
          "written, skipped and failed - failed means the address did not hold the bytes the "
          "file documents, which is a cheat file for another build.")
    return 1


if __name__ == "__main__":
    sys.exit(main())

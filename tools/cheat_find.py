# -*- coding: utf-8 -*-
"""Find a byte pattern in a running game, and say whether it is a usable address.

This is the friendly face of /api/mem/find. It exists because "where is this code?" is the first
question of making a cheat from scratch or moving one to a build nobody has a file for, and answering
it with curl means remembering that offsets are image-relative and that a match count of one is the
only answer you can act on.

    python tools/cheat_find.py CUSA01589 8B8370010000
    python tools/cheat_find.py CUSA01589 "48 8B 05 ?? ?? ?? ?? 89"      # ?? is a wildcard
    python tools/cheat_find.py CUSA01589 41898770010000 --from 0x500000 --to 0x600000
    python tools/cheat_find.py CUSA01589 --bytes-at 5BCBA5 --len 16     # read, do not search

WHAT A COUNT MEANS, and it is the only thing that matters:

    1 match          an address. Use it.
    0 and no gaps    that code is not in this build.
    0 WITH gaps      nothing was read there - a different answer, and the one this tool used to give
                     wrongly. A module's range has holes between its segments, and where the console
                     cannot report a module size the span is a guess, so gaps are ordinary.
    2 or more        NOT an address. Measured on a real module: the six bytes 8B8370010000 appear six
                     times in Dark Souls II. Picking one would put a cheat in the middle of an
                     unrelated function. Lengthen the pattern until exactly one survives.

Nothing is written. A full 34 MB sweep is about 230 seconds on a PS4 (4 KB per agent request at a
25 ms poll) and seconds on a PS5, so --from/--to are there to keep an exploratory search small.
"""
import argparse
import json
import sys
import time
import urllib.request

PC = "http://127.0.0.1:8710"
POLL_BUDGET_S = 20 * 60          # a full PS4 module is ~4 minutes; five times that is a dead console


def api(url, timeout=40):
    with urllib.request.urlopen(url, timeout=timeout) as f:
        return json.loads(f.read().decode())


def live_for(tid):
    """(platform, pid, base, console url) for a running title, or None.

    The console's address comes from the companion's /api/devices - never a constant in this file."""
    for con in ("ps4", "ps5"):
        try:
            d = api("%s/api/mods/%s?console=%s&state=1" % (PC, tid, con), timeout=60)
        except Exception:
            continue
        if d.get("running") and d.get("pid") and d.get("base"):
            try:
                dv = api("%s/api/devices" % PC, timeout=10)
            except Exception:
                return None
            for c in (dv.get("consoles") or []):
                if str(c.get("platform") or "").lower() == con and c.get("ip"):
                    return (con, int(d["pid"]), int(str(d["base"]), 16),
                            "http://%s:%d" % (c["ip"], int(c.get("shop_port") or 8710)))
    return None


def cancel(url):
    try:
        api("%s/api/mem/find/cancel" % url, timeout=20)
    except Exception:
        pass


def verdict(st, hits, gaps):
    """What the counts mean, as sentences. Separated so there is one place that decides."""
    if st.get("truncated"):
        return ["NOT USABLE. The match list was capped, so this is not the whole answer.",
                "Lengthen the pattern until it identifies far fewer places."]
    if len(hits) == 1:
        return ["ONE MATCH: %s is an address you can use." % hits[0]]
    if not hits and gaps:
        return ["CANNOT SAY. %d chunk(s) could not be read, so much of that range was never" % gaps,
                "searched. Narrow it with --from/--to to a range you know is code."]
    if not hits:
        return ["NOT IN THIS BUILD. Those bytes are not here, and every chunk was read."]
    return ["NOT AN ADDRESS. %d matches means this pattern does not identify one place;" % len(hits),
            "lengthen it with the bytes around the site until exactly one survives."]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("title_id")
    ap.add_argument("pattern", nargs="?", help="hex, with ?? / ** / xx as a wildcard byte")
    ap.add_argument("--from", dest="frm", default="0")
    ap.add_argument("--to", dest="to", default="0")
    ap.add_argument("--bytes-at", dest="at", help="read this image offset instead of searching")
    ap.add_argument("--len", dest="ln", type=int, default=16)
    a = ap.parse_args()

    live = live_for(a.title_id)
    if not live:
        print("%s is not running. This reads the game itself, so it has to be open." % a.title_id)
        return 1
    plat, pid, base, url = live
    print("%s on the %s: pid=%d base=0x%X" % (a.title_id, plat.upper(), pid, base))

    if a.at:
        off = int(a.at, 16)
        got = api("%s/api/mem/read?pid=%d&addr=0x%X&len=%d"
                  % (url, pid, base + off, min(256, max(1, a.ln))))
        if got.get("rc") != 0:
            print("  offset %X cannot be read (rc=%s)" % (off, got.get("rc")))
            return 1
        print("  offset %X = %s" % (off, got.get("hex")))
        return 0

    if not a.pattern:
        print("give a pattern to search for, or --bytes-at to read one place")
        return 2

    pat = "".join(a.pattern.split())
    frm, to = int(a.frm, 0), int(a.to, 0)
    q = "%s/api/mem/find?pattern=%s&pid=%d&base=0x%X" % (url, pat, pid, base)
    if frm or to:
        q += "&from=%d&to=%d" % (frm, to)
    r = api(q)
    if not r.get("ok"):
        print("  refused: %s" % (r.get("error") or r.get("rc")))
        return 1

    span = (r.get("to") or 0) - (r.get("from") or 0)
    note = "  searching %s bytes for %s" % ("{:,}".format(span), pat)
    if plat == "ps4":
        note += "  (about %ds)" % int(span / 150000)
    if r.get("span_guessed"):
        note += "  [the span is a guess: this console does not report a module size]"
    print(note)

    t0 = time.time()
    last = -1
    while True:
        if time.time() - t0 > POLL_BUDGET_S:
            print("")
            print("  it has not finished in %d minutes - cancelling" % (POLL_BUDGET_S // 60))
            cancel(url)
            return 1
        try:
            st = api("%s/api/mem/find/status" % url, timeout=30)
        except Exception:
            time.sleep(2)
            continue
        p = st.get("percent", 0)
        if p >= last + 20:
            last = p - (p % 20)
            sys.stdout.write("   %d%%" % p)
            sys.stdout.flush()
        if st.get("active"):
            time.sleep(3)
            continue

        print("")
        hits = st.get("offsets") or []
        gaps = st.get("gaps", 0)
        print("  %d match(es) in %.0fs, %d unreadable gap(s)%s"
              % (len(hits), time.time() - t0, gaps,
                 " - and it hit the match ceiling" if st.get("truncated") else ""))
        for h in hits:
            print("     %s" % h)
        print("")
        for line in verdict(st, hits, gaps):
            print("  %s" % line)
        return 0


if __name__ == "__main__":
    sys.exit(main())

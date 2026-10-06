# -*- coding: utf-8 -*-
"""Snapshot the companion's API surface, so "we did not break anything" is a test.

WHY THIS EXISTS. The PS5 half of this app is finished and must stay exactly as it is while the PS4
half and the two-console handling are worked on - and those live in the same file, the same routes
and the same page. "I did not touch the PS5 path" is an assertion; a recorded before-and-after of
every route's shape is evidence.

WHAT IT RECORDS is the SHAPE, not the contents: which keys exist, of what type, how many entries a
list has, and the values of fields that are decisions rather than readings (a platform, an id, a
port, a flag). Everything that moves on its own - uptimes, counters, free space, timestamps, ids,
progress - is replaced by its type. A snapshot that changed every run would be worthless.

    python tools/snapshot_api.py --save baseline-ps5.json     take a reference
    python tools/snapshot_api.py --check baseline-ps5.json     compare against it

Read-only: every route it calls is a GET, and the two that could do something (rescan, discover)
are deliberately left out.
"""
import argparse
import json
import sys
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8710"

# Every GET that describes state. Routes that ACT - rescan, discover, queue/start, install, notify,
# dpi/reload, rest/prepare, open-ps5, hosts/cleanup - are not here on purpose: a snapshot must never
# change the thing it is measuring.
ROUTES = [
    "/api/health",
    # THE BIGGEST ANSWER THIS APP GIVES, and it was not being watched. Every card, every panel and
    # every install decision is built from a game entry's shape, so a key quietly renamed here is
    # felt everywhere at once. A list is recorded as "how many, and what shape is the first", so a
    # library that grows by a game does not read as a change.
    "/api/library",
    "/api/consoles",
    "/api/devices",
    "/api/installed",
    "/api/storage",
    "/api/queue",
    "/api/config",
    "/api/sources",
    "/api/network",
    "/api/federation",
    "/api/federation/peers",
    "/api/engine/state",
    "/api/psn-block",
    "/api/cheats/paths",
    "/api/cheats/library",
    "/api/console/apps",
    "/api/payloads/autostart",
    "/api/ps4/tile",
    "/api/move/status",
]

# Fields whose VALUE is a reading of the world rather than a decision. Their type is recorded and
# their value discarded, so a snapshot taken a minute later still matches.
VOLATILE = {
    "uptime_s", "conns", "blocked", "forwarded", "last_blocked", "free", "used", "total",
    "size", "bytes", "pct", "done", "job_id", "task", "id", "ids", "device", "started_ms",
    "now", "ts", "time", "elapsed", "speed", "eta", "count", "n", "len", "mtime", "modified",
    "last_seen", "latency", "ms", "version_date", "progress", "transferred", "seen",
    # Found by running the harness twice across a change: all readings of the moment, none of them
    # a decision the code makes. A snapshot that flags these is a snapshot nobody will trust.
    "last_scan_ms", "library_gen", "scanning", "latency_ms", "tasks", "jobs", "generation",
    "started", "finished", "updated", "age", "uptime", "queued", "active", "running",
    # log_bytes grows with every install. "bytes" was already here and did not cover it - the set
    # matches whole key names, not substrings.
    #
    # NOT "local": /api/network reports a BOOLEAN under that name (is this PC the one serving),
    # which is a decision and has to stay in the snapshot, while /api/cheats/library uses the same
    # word for a count. One flat name-set cannot tell them apart, so the cheat counters stay in and
    # a diff on them means "the scan had not finished when one of these was taken", not a change.
    "log_bytes",
}


def shape(v, key=""):
    """A value reduced to what should not change: its type, and its value only when the value is
    a decision (a platform, a flag, a port, a short identifier chosen by us)."""
    if key in VOLATILE:
        return "<%s>" % type(v).__name__
    if isinstance(v, dict):
        return {k: shape(v[k], k) for k in sorted(v)}
    if isinstance(v, list):
        if not v:
            return []
        # Lists are recorded as "how many, and what shape is the first" - a library with 66 games
        # and one with 67 must not read as a regression.
        return ["<list len=%s>" % type(len(v)).__name__, shape(v[0])]
    if isinstance(v, str) and len(v) > 120:
        return "<str>"
    return v


def fetch(path, timeout=90):
    try:
        with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            code = r.status
    except urllib.error.HTTPError as e:
        raw, code = e.read().decode("utf-8", "replace"), e.code
    except Exception as e:
        return {"__error__": str(e)[:120]}
    try:
        return {"__status__": code, "body": shape(json.loads(raw))}
    except Exception:
        return {"__status__": code, "body": "<non-json len=%d>" % len(raw)}


def take():
    return {p: fetch(p) for p in ROUTES}


def diff(a, b, path=""):
    out = []
    if type(a) is not type(b):
        return ["%s: %s -> %s" % (path or "/", type(a).__name__, type(b).__name__)]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a:
                out.append("%s/%s: ADDED %s" % (path, k, json.dumps(b[k])[:90]))
            elif k not in b:
                out.append("%s/%s: REMOVED %s" % (path, k, json.dumps(a[k])[:90]))
            else:
                out += diff(a[k], b[k], "%s/%s" % (path, k))
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append("%s: list %d -> %d" % (path, len(a), len(b)))
        else:
            for i, (x, y) in enumerate(zip(a, b)):
                out += diff(x, y, "%s[%d]" % (path, i))
    elif a != b:
        out.append("%s: %s -> %s" % (path, json.dumps(a)[:70], json.dumps(b)[:70]))
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--save", metavar="FILE")
    ap.add_argument("--check", metavar="FILE")
    ap.add_argument("--print", action="store_true")
    a = ap.parse_args()
    snap = take()
    if a.print:
        print(json.dumps(snap, indent=1))
    if a.save:
        json.dump(snap, open(a.save, "w"), indent=1, sort_keys=True)
        ok = sum(1 for v in snap.values() if "__error__" not in v)
        print("saved %s - %d/%d routes answered" % (a.save, ok, len(snap)))
        return 0
    if a.check:
        old = json.load(open(a.check))
        d = diff(old, snap)
        if not d:
            print("no change in the API surface across %d routes" % len(snap))
            return 0
        print("%d difference(s) against %s:" % (len(d), a.check))
        for line in d:
            print("  " + line)
        return 1
    print("pass --save FILE or --check FILE")
    return 2


if __name__ == "__main__":
    sys.exit(main())

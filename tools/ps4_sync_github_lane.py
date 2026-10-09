# -*- coding: utf-8 -*-
"""Keep ps4-app/onconsole/github_lane.h honest against its source in ps5-app/onconsole/server.c.

WHAT THIS COPIES, AND WHY IT IS A COPY. Reading a project's releases and downloading one of its
files is not console-specific in any part: a TLS GET through sceHttp, a sha256, a scan for the
object bounds in a JSON document, and a cache of the answers. Both SDKs export the same sceHttp
and sceSsl entry points - verified in libSceHttp.so on each before a line of this was written -
so there is nothing for a PS4 version to do differently.

So the PS4 does not get a second implementation of it; it gets THIS one. Copied, because the PS5
file is shipping and restructuring it into a library is all downside - the same judgement
cheat_core.h and sqmini.h were made under. Copied by a script rather than by hand, because a hand
copy rots silently: the PS5 gains a fix, the PS4 keeps the bug, and nothing says so.

    python tools/ps4_sync_github_lane.py            # regenerate the copy from server.c
    python tools/ps4_sync_github_lane.py --check    # exit 1 if it has drifted (both ELF builds)

WHAT THE PS4 MUST STILL PROVIDE ITSELF, and the list is short on purpose:
    pms_dl_progress / pms_dl_cancelled / pms_dl_expected  - over its own download job record
    mkparents, ilog, json_escape, qparam, send_json       - which it has had since it was written
    json_str_after_lim, json_num_after                    - through cheat_core.h
    strcasestr_local                                      - which it has
    DL_BUF                                                - its own read/write buffer size
If a name goes missing from that list the PS4 build fails to link, loudly, which is the point.

The extractor is cheat_core.h's, imported rather than copied: it already handles a brace inside a
character literal (json_obj_end is written in terms of '{' and '}', and a naive counter truncates
it silently) and carries the comment block above each function, which is where the reasoning is.
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)

from ps4_sync_cheat_core import defn, region                   # noqa: E402

SRC = os.path.join(ROOT, "ps5-app", "onconsole", "server.c")
DST = os.path.join(ROOT, "ps4-app", "onconsole", "github_lane.h")

# Regions are types, tables and defines - things that are not functions and so cannot be found by
# name. Each `first` is a line that appears exactly once in server.c.
REGIONS = [
    # The sceNet/sceSsl/sceHttp entry points, every one of them checked against the SDK's own
    # exports before being declared.
    ("int sceNetPoolCreate(", "int sceHttpSetSendTimeOut("),
    # The read/write buffer both transports use. A define rather than a literal because it is a
    # measurement - see the comment that travels with it.
    ("#define DL_BUF", "#define DL_BUF"),
    # The sha256 context, its constant table and the rotate macro, with the note on what the
    # digest does and does not prove.
    ("/* ---------------- SHA-256 ---", "#define PMS_ROR"),
    # How big a release document this console will read, and one asset as it needs it.
    ("#define GH_BUF_BYTES", "#define GH_BUF_BYTES"),
    ("/* One file attached to a release", "#define UP_ASSETS"),
    # The cache itself: the type, the two limits and the three file statics the worker writes.
    ("/* ---------------- what projects have released", "static pthread_mutex_t g_up_lock"),
]

# Dependency order: emitted as one header with no forward declarations of its own, so a function
# must come after everything it calls that is also here.
FUNCS = [
    # The one call that makes TLS work on these consoles. Dereferences nothing, deliberately.
    "pms_ssl_accept_cb",
    # A write that says whether the bytes landed. The PS4 had only the void-returning write_all,
    # and "the counter climbed while every write failed" is a bug this file already carries the
    # scar of.
    "write_all_checked",
    # sha256
    "pms_sha256_init", "pms_sha256_block", "pms_sha256_update", "pms_sha256_final", "sha256_file",
    # which hosts this console's TLS can reach, and the two it cannot
    "host_unreachable_here",
    # the shipping GET and the shipping download
    "https_get_text", "https_download",
    # replacing the previous copy of a payload, without eating the one the build carries
    "dl_drop_same_stem",
    # which release this console took, written beside the file - the only way the panel can stop
    # offering an update that has already been taken, and the proof that THIS lane put a file in
    # the payload folder rather than some PC.
    "dl_note_path", "dl_take_note", "dl_taken_version",
    # reading a release document
    "json_obj_end", "release_choose", "github_releases", "release_assets",
    # the cache and its single worker
    "up_find", "up_slot", "upstream_worker", "up_enqueue",
]

HEAD = '''/* github_lane.h - the PS4's copy of the PS5's GitHub lane.
 *
 * GENERATED by tools/ps4_sync_github_lane.py from ps5-app/onconsole/server.c. DO NOT EDIT: the
 * next sync overwrites it, and a fix made here would be a fix the PS5 never gets. Change server.c
 * and re-run the tool. Both ELF builds run it with --check, so the two cannot drift apart quietly.
 *
 * Everything in here is the same on both consoles: a TLS GET through sceHttp, a sha256, finding
 * one release's bounds and its files inside a JSON document, and a six-hour cache of the answers
 * filled by one worker thread. Both SDKs export the same entry points.
 *
 * The PS4 must define, BEFORE including this:
 *   pms_dl_progress / pms_dl_cancelled / pms_dl_expected   over its own download job
 *   DL_BUF, mkparents, ilog, json_escape, write-side helpers
 *   json_str_after_lim and json_num_after                  (cheat_core.h provides both)
 *   strcasestr_local
 * Anything missing is a compile or link error, which is the intended failure.
 */
#pragma once
'''


def build():
    lines = io.open(SRC, encoding="utf-8", errors="replace").read().split("\n")
    out, missing = [HEAD], []

    for first, last in REGIONS:
        t = region(lines, first, last)
        if t is None:
            missing.append("region:%s" % first[:46])
        else:
            out.append(t)
            out.append("")

    for fn in FUNCS:
        t = defn(lines, fn)
        if t is None:
            missing.append(fn)
        else:
            out.append(t)
            out.append("")
        # PMS_ROR is #undef'd between the last sha256 function and sha256_file in server.c, so it
        # is re-stated here rather than carried: it is a token, not logic.
        if fn == "pms_sha256_final":
            out.append("#undef PMS_ROR")
            out.append("")

    return "\n".join(out), missing


def main():
    check = "--check" in sys.argv
    text, missing = build()
    if missing:
        print("ps4_sync_github_lane: NOT FOUND in server.c: %s" % ", ".join(missing))
        return 1
    have = io.open(DST, encoding="utf-8").read() if os.path.exists(DST) else ""
    if check:
        if have != text:
            print("ps4_sync_github_lane: github_lane.h has DRIFTED from server.c - "
                  "run tools/ps4_sync_github_lane.py")
            return 1
        print("ps4_sync_github_lane: in sync (%d functions, %d regions)"
              % (len(FUNCS), len(REGIONS)))
        return 0
    io.open(DST, "w", encoding="utf-8", newline="\n").write(text)
    print("ps4_sync_github_lane: wrote %s (%d functions, %d lines)"
          % (os.path.relpath(DST, ROOT), len(FUNCS), text.count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

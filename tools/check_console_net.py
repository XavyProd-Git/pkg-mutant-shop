# -*- coding: utf-8 -*-
"""Gate: the on-console servers must never be able to stop answering because a PC is switched off.

WHAT THIS IS ABOUT, MEASURED ON HARDWARE 2026-10-02.

The owner's laptop was off. The PS5 shop then did not work at all: the page would not load, the
browser left the bare URL in its title bar, and re-running the ELF said "already running" because
the port was still bound the whole time. A second, perfectly healthy PC on the same network did not
help. The PS4 was fine throughout. Starting the laptop fixed the PS5 instantly.

The cause was one line. ps5-app/onconsole/server.c serves every request INLINE on a single accept
loop, and /api/library merges each registered companion's library over HTTP from that loop. The
helper that does it set SO_RCVTIMEO and SO_SNDTIMEO and then called a plain blocking connect() -
and those two options bound read() and write(), NOT connect(). These headers are FreeBSD 11.4,
where an unanswered SYN runs net.inet.tcp.keepinit: 75 seconds. Worse, that loop was the ONE reader
of the companion table that did not apply the staleness rule every other reader applied, so a
laptop last seen hours earlier was still dialled on every cache miss. A healthy second PC could not
help because the loop blocks on the dead entry before it ever reaches the live one.

So this gate pins the invariants that keep the consoles independent of any PC:

  1. No raw connect() anywhere in the PS5 server. Every one goes through connect_deadline().
  2. The loop that dials companions skips the ones that have gone quiet, and has a total budget.
  3. The companion list handed to the page drops stale entries - it is what the page spends four
     seconds per entry on before falling back to the console's own API.
  4. The POST body read accounts for where the body STARTS inside the request buffer.
  5. The console can always serve its own UI, from the copy inside the ELF, when the disk will not.

Each of these has been perturbed and watched go red. A gate nobody has seen fail is not a gate.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PS5 = os.path.join(ROOT, "ps5-app", "onconsole", "server.c")
PS4 = os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c")

fails = []
n = 0


def ok(cond, what, detail=""):
    global n
    n += 1
    if not cond:
        fails.append("%s%s" % (what, (" - " + detail) if detail else ""))


def read(p):
    return io.open(p, encoding="utf-8", errors="replace").read()


def strip_comments(src):
    """Comments quote the old code on purpose; they must not satisfy a check.

    NEWLINES ARE KEPT. Collapsing a block comment to a single space renumbers every line after it,
    and this gate prints line numbers for a person to go and look at - the first run of it pointed
    at two lines that had nothing whatever to do with the finding."""
    def blank(m):
        return "\n" * m.group(0).count("\n")
    src = re.sub(r"/\*.*?\*/", blank, src, flags=re.S)
    return re.sub(r"//[^\n]*", "", src)


def main():
    import io as _io2
    ps5 = read(PS5)
    ps5_code = strip_comments(ps5)
    ps4 = read(PS4)
    ps4_code = strip_comments(ps4)

    # ---- 1. EVERY OUTBOUND CONNECT IS BOUNDED ---------------------------------------------------
    ok("static int connect_deadline(" in ps5_code,
       "the PS5 server has a connect() with a deadline")
    ok("O_NONBLOCK" in ps5_code and "SO_ERROR" in ps5_code,
       "...implemented as a non-blocking connect checked for its real error",
       "SO_RCVTIMEO/SO_SNDTIMEO do not bound connect(); only this does")

    # A raw `connect(` that is not the one inside connect_deadline, and not a UDP route lookup.
    lines = ps5_code.splitlines()
    # The one raw connect() that MUST exist is the one inside connect_deadline itself. Find its
    # body so hits within it are not reported - matching only on the text of the line missed it,
    # because that line reads plainly `int rc = connect(s, sa, slen);`.
    helper_lo = helper_hi = -1
    hstart = ps5_code.find("static int connect_deadline(")
    if hstart >= 0:
        hend = ps5_code.find("\nstatic ", hstart + 10)
        helper_lo = ps5_code[:hstart].count("\n")
        helper_hi = ps5_code[:hend if hend > 0 else len(ps5_code)].count("\n")
    offenders = []
    for m in re.finditer(r"(?<![a-z_])connect\s*\(", ps5_code):
        ln = ps5_code[:m.start()].count("\n")
        text = lines[ln] if ln < len(lines) else ""
        if "connect_deadline" in text:
            continue                       # a call TO the helper
        if helper_lo <= ln <= helper_hi:
            continue                       # the helper's own, which is the whole point of it
        # lan_ip_str() connects a SOCK_DGRAM socket to read the routing table. No packet is sent
        # and no handshake happens, so it cannot block; it is the one legitimate raw connect.
        ctx = "\n".join(lines[max(0, ln - 12):ln + 1])
        if "SOCK_DGRAM" in ctx:
            continue
        offenders.append(ln + 1)
    ok(not offenders,
       "no raw connect() is left in the PS5 server - they all go through the deadline",
       "lines %s" % offenders)

    # ---- 2. THE DIAL LOOP ------------------------------------------------------------------------
    ok("#define PC_DIAL_STALE_MS" in ps5_code,
       "there is a separate, shorter silence before a companion stops being dialled")
    ok("PC_DIAL_STALE_MS" in ps5_code and "PC_FAIL_BACKOFF_MS" in ps5_code,
       "...and a backoff after one that refuses")
    ok("#define PEER_MERGE_BUDGET_MS" in ps5_code,
       "...and a ceiling on the whole merge, whatever the table holds")

    merge = ps5_code.split("merge_pc_library(local_copy[i].ip", 1)
    ok(len(merge) == 2, "the companion merge loop is where it was expected")
    if len(merge) == 2:
        before = merge[0][-1400:]
        ok("PC_DIAL_STALE_MS" in before,
           "the loop that DIALS a companion skips the ones that have gone quiet",
           "this was the one reader of the table without the rule, and the only one that opens a socket")
        ok("PC_FAIL_BACKOFF_MS" in before,
           "...and does not re-dial one that just refused")
        ok("PEER_MERGE_BUDGET_MS" in before,
           "...and gives up on the rest once the merge has cost enough")

    # ---- 3. WHAT THE PAGE IS OFFERED -------------------------------------------------------------
    for src, code, name in ((PS5, ps5_code, "PS5"), (PS4, ps4_code, "PS4")):
        head = code.split('"/api/companion"', 1)
        if len(head) == 2:
            body = head[1][:2200]
            ok("PC_STALE_MS" in body,
               "%s /api/companion drops companions that have gone quiet" % name,
               "the page spends four seconds on each one before it gives up")
    srcs = ps4_code.split('"/api/sources"', 1)
    if len(srcs) == 2:
        ok("PC_STALE_MS" in srcs[1][:1200],
           "PS4 /api/sources does not show a green light for a PC that left")

    # ---- 4. THE POST BODY ------------------------------------------------------------------------
    ok("POST_BODY_MAX" in ps5_code, "the PS5 server caps how large a POST body may be")
    # The accept loop's OWN body read, not the first content-length in the file - there are five,
    # and the one this is about is the request buffer on the serving path.
    post = ps5_code.split('strcasestr_local(buf, "content-length:")', 1)
    if len(post) == 2:
        blk = post[1][:1800]
        # THE `room =` LINE ITSELF, not "is (body - buf) mentioned anywhere near here". The first
        # version of this check asked the looser question and happily passed with the subtraction
        # deleted - because `int have = n - (int)(body - buf);` two lines up still matched it. The
        # defect it is supposed to catch is a remote stack overflow, so it gets the exact question.
        room_line = ""
        for ln_ in blk.splitlines():
            if re.search(r"\broom\s*=", ln_) and "sizeof(buf)" in ln_:
                room_line = ln_.strip()
                break
        ok(bool(room_line) and "body - buf" in room_line,
           "the POST body read measures the room LEFT after the headers",
           "comparing Content-Length against the WHOLE buffer overflows it by the header length; "
           "room line was %r" % (room_line or "(not found)"))
        ok("413" in blk, "...and refuses one that is too large rather than writing past the end")

    # ---- 5. THE CONSOLE CAN ALWAYS SHOW ITS OWN PAGE ---------------------------------------------
    ok("serve_embedded(" in ps5_code,
       "the PS5 can serve the UI from inside the ELF when the disk will not")
    stat = ps5_code.split("static void serve_static(", 1)
    if len(stat) == 2:
        ok("serve_embedded(" in stat[1][:900],
           "...and serve_static actually falls back to it before answering 404")

    # ---- 6. THE CONSOLE'S OWN ADDRESS ------------------------------------------------------------
    lan = ps5_code.split("const char *lan_ip_str(void) {", 1)
    if len(lan) == 2:
        blk = lan[1][:1200]
        ok('strcmp(ip, "127.0.0.1")' in blk,
           "the console never remembers the loopback fallback as its own LAN address",
           "it is published to every PC as where to reach this console")

    # ---- 7. AND THE PS4 KEEPS THE SHAPE THAT MADE IT IMMUNE ---------------------------------------
    ok("pthread_create(&t, &at, conn_thread, c)" in ps4_code
       or "conn_thread" in ps4_code,
       "the PS4 still gives every connection its own thread",
       "that is the only reason it survived the fault this gate is about")

    # ---- 8. THE BYTES MOVE AT THE SPEED OF THE CABLE ---------------------------------------------
    # Measured on this hardware 2026-10-02. A 128 MB write to the PS5's /mnt/ext1: 42.7 MB/s with a
    # 64 KB buffer, 78.6 with 1 MB, 108.7 with 1 MB plus socket buffers - which is the link, and the
    # same speed the console reads at. /data went 16.1 -> 74.3. None of this is visible in any test
    # that only asks whether a transfer SUCCEEDS, so it is pinned here.
    for name, code in (("PS5", ps5_code), ("PS4", ps4_code)):
        ok("SO_RCVBUF" in code and "SO_SNDBUF" in code,
           "%s asks for a real socket buffer" % name,
           "without it the same transfer runs at 78.6 MB/s instead of 108.7")
    ok("#define COPY_BUF_BYTES (1 << 20)" in ps5_code,
       "the PS5 moves a megabyte per syscall on a bulk path",
       "64 KB costs 16x the read/write pairs and most of the throughput")
    ok("#define DL_BUF         (1 << 20)" in ps5_code or "#define DL_BUF (1 << 20)" in ps5_code,
       "...and so does the path it DOWNLOADS a game through",
       "the listening socket's buffers do not reach an outbound socket; this one is its own")
    dl = ps5_code.split("static int tcp_connect_host(", 1)
    if len(dl) == 2:
        ok("SO_RCVBUF" in dl[1][:900],
           "the console's own download socket gets a receive window",
           "an install PULLS - this is the socket a 50 GB game arrives through")
    pk = ps5_code.split("static void *pkgfile_thread(", 1)
    if len(pk) == 2:
        ok("SO_SNDTIMEO" in pk[1][:1200],
           "serving a package clears the accept loop's send timeout",
           "the installer stops reading while it promotes, for longer than 30s on a big title")
    ok("BULK_BUF_BYTES" in ps4_code and "bulk_buf_get(" in ps4_code,
       "the PS4 asks for a big buffer and falls back when its heap says no")
    ok("write_all(f, buf, (size_t)r)" in ps4_code,
       "the PS4 upload loop treats a short write as normal, not as a failure",
       "write() may legally write fewer bytes; this used to discard a multi-GB transfer")

    # ---- 9. AND THE FAST LANE IS REACHABLE -------------------------------------------------------
    # _run_mount decides pull-vs-push from t["url"] or t["key"], and the job for a backup on THIS PC
    # set neither - so the console was pushed to at 16.9 MB/s while the code to have it pull at 110
    # sat there unreachable. Measured off the owner's own console log: Marvel's Wolverine, 112 GB,
    # 1h50m pushed against about 17 minutes pulled.
    _srv = _io2.open(os.path.join(ROOT, "companion", "server.py"), encoding="utf-8").read()
    _mnt = _srv.split('"local_path": path, "dest": dest,', 1)
    ok(len(_mnt) == 2, "the local backup mount job is where it was expected")
    if len(_mnt) == 2:
        ok('"key": key,' in _mnt[1][:1600],
           "a backup on THIS PC is PULLED by the console, not pushed to it",
           "without the key _run_mount cannot build a url and silently falls back to the slow push")

    print("")
    if fails:
        print("check_console_net: FAIL")
        for f in fails:
            print("   " + f)
        return 1
    print("check_console_net: OK (%d checks)" % n)
    return 0


if __name__ == "__main__":
    sys.exit(main())

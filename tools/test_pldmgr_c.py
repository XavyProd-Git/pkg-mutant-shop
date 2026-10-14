#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate: the PS5 payload talks to Payload Manager 0.5.x AND 0.6.0, and writes where each one looks.

WHAT CHANGED UNDER US. Payload Manager 0.6.0 (measured on the owner's PS5 the day it came out):
  * a GET of /loadpayload or /process_kill answers 405 - its own page sends both as a POST with
    "X-PLDMGR-Request: 1". Every load the payload made was that GET, which is how a console on
    0.6.0 could not start our installer (no installs at all) and FTP did not come back at boot;
  * every payload lives in /data/pldmgr/payloads/local/<slot>/ with a meta.json, .layout says "2",
    and the autoload list names slots - so a copy written to the old folder is read by nothing.

HOW THIS TESTS IT. The functions are LIFTED OUT OF ps5-app/onconsole/server.c BY TEXT and compiled in
WSL (the payload's socket and file code is POSIX, which a Windows compiler would only imitate):
  * pm_send / pm_get against a Payload Manager forked off inside the test binary, answering as
    0.5.x (GET only) and as 0.6.0 (POST + header only);
  * pb_stage_named against a temporary /data, laid out as 0.5.x and as 0.6.0.
Skips (exit 0, and says so) where WSL or its compiler is not there.

    python tools/test_pldmgr_c.py
"""
import io
import os
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
SRC = os.path.join(ROOT, "ps5-app", "onconsole", "server.c")

from ps4_sync_cheat_core import defn, region                   # noqa: E402

PRELUDE = r'''
#define _GNU_SOURCE
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <time.h>
#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <errno.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/socket.h>
#include <sys/wait.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <signal.h>

static int g_pm_port;
#define PLDMGR_PORT g_pm_port
static char g_log[8192];
static void ilog(const char *fmt, ...) {
    char b[600]; va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof(b), fmt, ap); va_end(ap);
    strncat(g_log, b, sizeof(g_log) - strlen(g_log) - 2); strcat(g_log, "\n");
}
static void write_all(int fd, const char *buf, size_t len) {
    while (len) { ssize_t w = write(fd, buf, len); if (w <= 0) return; buf += w; len -= (size_t)w; }
}
static int connect_local(int port, int rcv_ms, int snd_ms) {
    (void)rcv_ms; (void)snd_ms;
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return -1;
    struct sockaddr_in a; memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET; a.sin_port = htons((unsigned short)port);
    a.sin_addr.s_addr = htonl(0x7f000001);
    if (connect(s, (struct sockaddr *)&a, sizeof(a)) != 0) { close(s); return -1; }
    return s;
}
'''

DRIVER = r'''
static int fails, checks;
static void ok(int c, const char *what) { checks++; if (!c) { fails++; printf("FAIL: %s\n", what); } }

/* A Payload Manager in a child process: answers `n` requests as `mode` ("0.5" or "0.6") and
   writes one line per request - "<METHOD> <has header>" - to the pipe. */
static int fake_pm(const char *mode, int n, int *pipe_rd) {
    int ls = socket(AF_INET, SOCK_STREAM, 0);
    int one = 1; setsockopt(ls, SOL_SOCKET, SO_REUSEADDR, &one, sizeof(one));
    struct sockaddr_in a; memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET; a.sin_addr.s_addr = htonl(0x7f000001); a.sin_port = 0;
    bind(ls, (struct sockaddr *)&a, sizeof(a)); listen(ls, 8);
    socklen_t al = sizeof(a); getsockname(ls, (struct sockaddr *)&a, &al);
    int p[2]; if (pipe(p) != 0) return -1;
    pid_t pid = fork();
    if (pid == 0) {
        close(p[0]);
        for (int i = 0; i < n; i++) {
            int c = accept(ls, NULL, NULL);
            if (c < 0) break;
            char req[2048]; size_t got = 0;
            for (;;) {
                ssize_t r = read(c, req + got, sizeof(req) - 1 - got);
                if (r <= 0) break;
                got += (size_t)r; req[got] = 0;
                if (strstr(req, "\r\n\r\n")) break;
            }
            req[got] = 0;
            int post = !strncmp(req, "POST ", 5);
            int hdr = strstr(req, "\r\nX-PLDMGR-Request: 1\r\n") != NULL;
            char line[32]; snprintf(line, sizeof(line), "%s %d\n", post ? "POST" : "GET", hdr);
            write_all(p[1], line, strlen(line));
            const char *resp;
            if (!strcmp(mode, "0.6"))
                resp = !post ? "HTTP/1.0 405 Method Not Allowed\r\nContent-Length: 18\r\n\r\nMethod Not Allowed"
                     : !hdr ? "HTTP/1.0 403 Forbidden\r\nContent-Length: 9\r\n\r\nForbidden"
                     : "HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nOK";
            else
                resp = post ? "HTTP/1.0 405 Method Not Allowed\r\nContent-Length: 18\r\n\r\nMethod Not Allowed"
                     : "HTTP/1.0 200 OK\r\nContent-Length: 2\r\n\r\nOK";
            write_all(c, resp, strlen(resp));
            close(c);
        }
        close(p[1]);
        _exit(0);
    }
    close(ls); close(p[1]);
    *pipe_rd = p[0];
    return ntohs(a.sin_port);
}

static void slurp(int fd, char *out, size_t sz) {
    size_t got = 0; ssize_t r;
    while (got + 1 < sz && (r = read(fd, out + got, sz - 1 - got)) > 0) got += (size_t)r;
    out[got] = 0;
}

static void put(const char *path, const char *body) {
    FILE *f = fopen(path, "wb"); fputs(body, f); fclose(f);
}
static long fsize(const char *path) { struct stat st; return stat(path, &st) ? -1 : (long)st.st_size; }
static int exists(const char *p) { struct stat st; return stat(p, &st) == 0; }

int main(void) {
    signal(SIGPIPE, SIG_IGN);
    char seen[256];
    int rd;

    /* ---- 0.6.0: one POST with the header ---- */
    g_pm_port = fake_pm("0.6", 1, &rd);
    int rc = pm_get("/loadpayload:/data/pldmgr/payloads/pms-installer/pms-installer.elf");
    wait(NULL); slurp(rd, seen, sizeof(seen)); close(rd);
    ok(rc == 0 && !strcmp(seen, "POST 1\n"), "0.6.0: a load is one POST carrying X-PLDMGR-Request");

    /* ---- 0.5.x: the POST is refused for its method, and the GET it always was follows ---- */
    g_pm_port = fake_pm("0.5", 2, &rd);
    rc = pm_get("/process_kill?pid=77");
    wait(NULL); slurp(rd, seen, sizeof(seen)); close(rd);
    ok(rc == 0 && !strcmp(seen, "POST 1\nGET 0\n"), "0.5.x: the POST is refused, the old GET works");
    ok(strstr(g_log, "405") == NULL, "...and the refusal of the POST is not logged as a failure");

    /* ---- nobody there ---- */
    g_pm_port = 1;
    ok(pm_get("/loadpayload:/x.elf") != 0, "no Payload Manager is a failure, not a success");

    /* ---- the 0.5.x layout ---- */
    char src[512], out[700];
    snprintf(src, sizeof(src), "%s/src-ftpsrv.elf", ROOTDIR);
    put(src, "ELF-ftpsrv-bytes-0123456789");
    rc = pb_stage_named(src, "ftpsrv-ps5.elf", out, sizeof(out));
    char want[700];
    snprintf(want, sizeof(want), "%s/data/pldmgr/payloads/ftpsrv-ps5/ftpsrv-ps5.elf", ROOTDIR);
    ok(rc == 0 && !strcmp(out, want) && fsize(want) == 27, "0.5.x: a payload goes where it always went");

    /* ---- the 0.6.0 layout: .layout says 2 and the owner's slots exist ---- */
    char d[700];
    snprintf(d, sizeof(d), "%s/data/pldmgr/payloads/.layout", ROOTDIR); put(d, "2\n");
    snprintf(d, sizeof(d), "%s/data/pldmgr/payloads/local", ROOTDIR); mkdir(d, 0777);
    snprintf(d, sizeof(d), "%s/data/pldmgr/payloads/local/ftpsrv", ROOTDIR); mkdir(d, 0777);
    snprintf(d, sizeof(d), "%s/data/pldmgr/payloads/local/ftpsrv/meta.json", ROOTDIR);
    put(d, "{\n  \"schema\": 2,\n  \"kind\": \"local\",\n  \"filename\": \"ftpsrv-ps5.elf\",\n  \"origin\": \"web_upload\"\n}\n");
    snprintf(d, sizeof(d), "%s/data/pldmgr/payloads/local/ftpsrv/ftpsrv-ps5.elf", ROOTDIR); put(d, "old");
    rc = pb_stage_named(src, "ftpsrv.elf", out, sizeof(out));
    ok(rc == 0 && !strcmp(out, d) && fsize(d) == 27,
       "0.6.0: a payload with a slot is written INTO it, under the slot's own filename");
    snprintf(want, sizeof(want), "%s/data/pldmgr/payloads/local/ftpsrv/ftpsrv.elf", ROOTDIR);
    ok(!exists(want), "...and no second file appears beside it");
    snprintf(want, sizeof(want), "%s/data/pldmgr/payloads/local/ftpsrv/meta.json", ROOTDIR);
    ok(fsize(want) > 60, "...and the owner's meta.json is left as it was");

    /* a payload with no slot: a new one, with Payload Manager's meta.json */
    char src2[512];
    snprintf(src2, sizeof(src2), "%s/src-PS4Mount.elf", ROOTDIR);
    put(src2, "ELF-ps4mount");
    rc = pb_stage_named(src2, "PS4Mount.elf", out, sizeof(out));
    snprintf(want, sizeof(want), "%s/data/pldmgr/payloads/local/ps4mount/PS4Mount.elf", ROOTDIR);
    ok(rc == 0 && !strcmp(out, want) && fsize(want) == 12, "0.6.0: a payload with no slot gets one");
    snprintf(want, sizeof(want), "%s/data/pldmgr/payloads/local/ps4mount/meta.json", ROOTDIR);
    char meta[600] = {0};
    FILE *mf = fopen(want, "rb"); if (mf) { size_t r = fread(meta, 1, sizeof(meta) - 1, mf); meta[r] = 0; fclose(mf); }
    ok(strstr(meta, "\"schema\": 2") && strstr(meta, "\"filename\": \"PS4Mount.elf\"")
       && strstr(meta, "\"kind\": \"local\""), "...with a meta.json in Payload Manager's own shape");

    /* the same bytes twice: nothing rewritten */
    struct stat a1, a2; stat(out, &a1);
    sleep(1);
    rc = pb_stage_named(src2, "PS4Mount.elf", out, sizeof(out));
    stat(out, &a2);
    ok(rc == 0 && a1.st_mtime == a2.st_mtime, "the same payload twice is not written twice");

    /* names that are paths are refused, whichever layout */
    ok(pb_stage_named(src2, "../x.elf", out, sizeof(out)) != 0
       && pb_stage_named(src2, "a/b.elf", out, sizeof(out)) != 0
       && pb_stage_named(src2, "x.bin", out, sizeof(out)) != 0,
       "a name that is a path, or not an ELF, is never written");

    printf("%d check(s), %d failure(s)\n", checks, fails);
    return fails ? 1 : 0;
}
'''

FUNCS = ("json_escape", "path_ext_is", "pm_stem", "pm_layout2", "pm_slot_filename", "pm_slot_find",
         "pm_slot_meta", "pb_stage_named", "pm_send", "pm_get")


def main():
    if not shutil.which("wsl"):
        print("SKIP: no WSL here - this gate compiles the payload's POSIX code there")
        return 0
    lines = io.open(SRC, encoding="utf-8", errors="replace").read().split("\n")
    parts = []
    t = region(lines, "static const char PM_HDR_TAIL[]", "'C','o','n'")
    if t is None:
        print("FAIL: PM_HDR_TAIL not found in server.c")
        return 1
    parts.append(t)
    for fn in FUNCS:
        t = defn(lines, fn)
        if t is None:
            print("FAIL: %s not found in server.c" % fn)
            return 1
        parts.append(t)
    body = "\n\n".join(parts)
    n = body.count('"/data/pldmgr')
    if n < 6:
        print("FAIL: the payload's Payload Manager paths are not where this test expects them (%d)" % n)
        return 1
    body = body.replace('"/data/pldmgr', 'ROOTDIR "/data/pldmgr')
    work = tempfile.mkdtemp(prefix="pms-pm-")
    try:
        root = os.path.join(work, "root")
        os.makedirs(os.path.join(root, "data"))
        io.open(os.path.join(work, "t.c"), "w", encoding="utf-8", newline="\n").write(
            PRELUDE + "\n" + body + "\n" + DRIVER)

        def wsl_path(p):
            p = os.path.abspath(p).replace("\\", "/")
            return "/mnt/%s%s" % (p[0].lower(), p[2:])

        wroot, wc = wsl_path(root), wsl_path(os.path.join(work, "t.c"))
        cmd = ('cc -O1 -w -DROOTDIR=\'"%s"\' -o /tmp/pms-pm-test "%s" && /tmp/pms-pm-test; rc=$?; '
               'rm -f /tmp/pms-pm-test; exit $rc' % (wroot, wc))
        r = subprocess.run(["wsl", "-e", "bash", "-lc", cmd], capture_output=True, text=True,
                           timeout=300)
        out = ((r.stdout or "") + (r.stderr or "")).strip()
        if "command not found" in out and "cc" in out:
            print("SKIP: WSL has no C compiler")
            return 0
        last = out.splitlines()[-1] if out else ""
        if r.returncode != 0 or "0 failure(s)" not in last:
            print("test_pldmgr_c: FAIL")
            print("  " + out[-3000:].replace("\n", "\n  "))
            return 1
        print("test_pldmgr_c: OK (%s)" % last)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

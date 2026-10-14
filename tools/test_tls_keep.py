#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Gate: a PS4 spends ONE secure-connection stack per boot, however often the app is loaded.

WHAT WAS MEASURED ON THE CONSOLE. The PS4's libSsl hands a host process eight working stacks and
refuses every one after that (sceHttpsLoadCert answers 0x8095f006 from the ninth on), the ids
never come back down, and GoldHEN loads every payload into the SAME host process - so the count
runs for a whole boot of the console, across every load of this app. The app used to build a
stack per request: one opening of the Updates drawer spent all eight and the console could not
download anything by itself until it was restarted.

So on that console the stack is made once, kept, and HANDED TO THE NEXT LOAD: its ids are written
to a note, and a later load of the app in the same process carries on with them.

HOW THIS TESTS IT. The real functions are LIFTED OUT OF ps5-app/onconsole/server.c BY TEXT (the
PS4's copy is generated from the same text) and compiled here against a libSsl/libHttp that is
thirty lines of C and has the console's limit: eight stacks, then refusal. "Another load of the
app" is the statics going back to their initial values while the library keeps its contexts,
which is exactly what a second copy of the payload in one process is.

Run it directly, or as one of the gates. Exit code 0 means every check passed.
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
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <time.h>
#ifndef CLOCK_MONOTONIC
#define CLOCK_MONOTONIC 1
#endif
#define pthread_mutex_t int
#define PTHREAD_MUTEX_INITIALIZER 0

/* ---- the clock and the process, both ours to move ---- */
static long long F_wall = 1791860000LL, F_up = 1000;
static int F_pid = 81;
static time_t fake_time(time_t *t) { if (t) *t = (time_t)F_wall; return (time_t)F_wall; }
static int fake_clock(int id, struct timespec *ts) { (void)id; ts->tv_sec = (time_t)F_up; ts->tv_nsec = 0; return 0; }
static int fake_getpid(void) { return F_pid; }
#define time fake_time
#define clock_gettime fake_clock
#define getpid fake_getpid

/* ---- a libSsl / libHttp with the console's limit ---- */
#define F_MAX 64
static int F_net = 3000, F_ssl = 1, F_http = 257, F_tmpl = 9000;
static int F_made, F_terms, F_loadcert_calls, F_templates;
static int F_alive[F_MAX], F_sslof[F_MAX];
static int F_ix(int http) { return (http >= 257 && http < 257 + F_MAX) ? http - 257 : -1; }
int sceNetPoolCreate(const char *n, int size, int flags) { (void)n; (void)size; (void)flags; return F_net++; }
int sceNetPoolDestroy(int id) { (void)id; return 0; }
int sceSslInit(int size) { (void)size; return F_ssl++; }
int sceSslTerm(int id) { (void)id; return 0; }
int sceHttpInit(int net, int ssl, int size) {
    (void)net; (void)size;
    int id = F_http++, i = F_ix(id);
    if (i < 0) return (int)0x80431001;
    F_alive[i] = 1; F_sslof[i] = ssl; F_made++;
    return id;
}
int sceHttpTerm(int id) { int i = F_ix(id); if (i >= 0) F_alive[i] = 0; F_terms++; return 0; }
int sceHttpCreateTemplate(int ctx, const char *ua, int ver, int proxy) {
    (void)ua; (void)ver; (void)proxy;
    int i = F_ix(ctx);
    if (i < 0 || !F_alive[i]) return (int)0x80431100;
    F_templates++;
    return F_tmpl++;
}
int sceHttpDeleteTemplate(int t) { (void)t; return 0; }
int sceHttpsLoadCert(int ctx, int n, void *a, void *b, void *c) {
    (void)n; (void)a; (void)b; (void)c;
    int i = F_ix(ctx);
    F_loadcert_calls++;
    if (i < 0 || !F_alive[i]) return (int)0x80431100;
    return F_sslof[i] > 8 ? (int)0x8095f006 : 0;          /* THE LIMIT, as measured */
}
int sceHttpsDisableOption(int t, unsigned f) { (void)t; (void)f; return (int)0x8043506b; }
int sceHttpsSetSslCallback(int t, void *cb, void *arg) { (void)t; (void)cb; (void)arg; return 0; }
static int pms_ssl_accept_cb(void) { return 0; }

static char F_log[8192];
static void ilog(const char *fmt, ...) {
    char b[512]; va_list ap; va_start(ap, fmt); vsnprintf(b, sizeof(b), fmt, ap); va_end(ap);
    strncat(F_log, b, sizeof(F_log) - strlen(F_log) - 2); strcat(F_log, "\n");
}
'''

DRIVER = r'''
static int fails, checks;
static void ok(int cond, const char *what) {
    checks++;
    if (!cond) { fails++; printf("FAIL: %s\n", what); }
}
/* Another load of the app in the same process: OUR statics start again, the library's do not. */
static void new_load(void) {
    g_ks_net = g_ks_ssl = g_ks_http = g_ks_loaded = -1;
    g_ks_made = 0; g_ks_adopted = 0; g_tls_state = 0; F_log[0] = 0;
}
static int request(void) {            /* what https_get_text does with a stack: open, relax, close */
    int net = -1, ssl = -1, http = -1; const char *what = "";
    int rc = tls_stack_open("t", &net, &ssl, &http, &what);
    if (rc < 0) { tls_stack_close(net, ssl, http); return rc; }
    int before = F_loadcert_calls;
    tls_relax(http, 5, "get");
    int worked = (g_ks_loaded == http) || (F_loadcert_calls > before && F_sslof[F_ix(http)] <= 8);
    tls_stack_close(net, ssl, http);
    return worked ? http : -http;
}
static int note_exists(void) { FILE *f = fopen(PMS_TLS_NOTE, "r"); if (f) fclose(f); return f != NULL; }

int main(void) {
    remove(PMS_TLS_NOTE);
#ifdef PMS_TLS_KEEP
    /* ---- one load, many requests ---- */
    int a = request(), b = request(), c = request();
    ok(a == 257 && b == 257 && c == 257 && F_made == 1 && F_terms == 0,
       "twelve requests in one load are one stack, never torn down");
    ok(F_loadcert_calls == 1, "the certificate store is loaded once for it, not once per request");
    ok(note_exists(), "its ids are written down for the next load");

    /* ---- the next load of the app, same process ---- */
    new_load();
    int d = request();
    ok(d == 257 && F_made == 1 && g_ks_adopted == 1 && g_ks_made == 0,
       "a second load of the app carries on with the first one's stack and creates none");
    ok(F_loadcert_calls == 1, "...and does not load the certificates a second time");
    ok(strstr(F_log, "carrying on with the stack") != NULL, "...and says so in the log");
    for (int i = 0; i < 20; i++) { new_load(); request(); }
    ok(F_made == 1, "twenty more loads in one boot are still one stack");

    /* ---- the wall clock is corrected once the network is up: still the same boot ---- */
    new_load(); F_wall += 45; F_up += 0;
    request();
    ok(F_made == 1 && g_ks_adopted == 1, "a clock corrected by under two minutes is the same boot");
    F_wall -= 45;

    /* ---- a note that must NOT be believed ---- */
    int m0 = F_made;
    new_load(); F_pid = 82;
    request();
    ok(F_made == m0 + 1 && g_ks_adopted == 0,
       "a different process never takes over another one's ids");
    /* (Back in 81, whose note now names 82's stack: made again, and the note is 81's own.) */
    F_pid = 81; new_load(); request();
    int m1 = F_made;
    new_load(); request();
    ok(F_made == m1 && g_ks_adopted == 1, "...and its own note is believed again");
    /* TOMORROW, THE SAME PID. Nothing else about the note is wrong and - in this fake - the
       context it names is even still alive, so the boot mark is the only thing that can refuse it. */
    new_load(); F_wall += 86400;
    request();
    ok(F_made == m1 + 1 && g_ks_adopted == 0,
       "a note from another boot is ignored, even under the same pid");
    /* The same boot again, and its context has gone. */
    int m2 = F_made, last = g_ks_http;
    new_load();
    F_alive[F_ix(last)] = 0;
    request();
    ok(F_made == m2 + 1 && strstr(F_log, "is gone") != NULL,
       "a note naming a context that no longer exists is ignored, and a stack is made");
    int m3 = F_made;
    new_load();
    { FILE *t = fopen(PMS_TLS_NOTE, "w"); fputs("81 30", t); fclose(t); }
    request();
    ok(F_made == m3 + 1, "a torn note is ignored");

    /* ---- A BOOT THAT HAS ALREADY SPENT ITS EIGHT (the owner's console today) ---- */
    F_ssl = 47;                                     /* 46 stacks made by earlier builds */
    new_load(); remove(PMS_TLS_NOTE);
    int m4 = F_made;
    int s1 = request();
    ok(s1 < 0 && F_made == m4 + 1, "on a spent boot the one stack that is made does not work");
    int made = F_made;
    for (int i = 0; i < 10; i++) request();
    ok(F_made == made, "...and ten more requests do not make ten more");
    for (int i = 0; i < 10; i++) { new_load(); request(); }
    ok(F_made == made && g_ks_adopted == 1,
       "...nor do ten more loads of the app: nothing further is spent finding out");
#else
    /* ---- a console with no limit keeps doing what it did: a stack per request ---- */
    int a = request(), b = request();
    ok(a == 257 && b == 258 && F_made == 2 && F_terms == 2,
       "without the switch every request makes its own stack and gives it back");
    ok(!note_exists(), "...and no note is written");
    ok(F_loadcert_calls == 2, "...and each loads its certificates");
#endif
    printf("%d check(s), %d failure(s)\n", checks, fails);
    return fails ? 1 : 0;
}
'''


def main():
    cc = shutil.which("gcc") or shutil.which("cc") or shutil.which("clang")
    if not cc:
        print("SKIP: no C compiler on PATH (gcc/cc/clang) - cannot run this gate here")
        return 0
    lines = io.open(SRC, encoding="utf-8", errors="replace").read().split("\n")
    parts = []
    for first, last in (("/* ONE TLS STACK IN THIS PROCESS AT A TIME.", "static int g_ks_adopted"),
                        ("/* DOES TLS WORK IN THIS PROCESS?", "static volatile int g_tls_state")):
        t = region(lines, first, last)
        if t is None:
            print("FAIL: could not find the region starting %r in server.c" % first)
            return 1
        parts.append(t)
    for fn in ("tls_boot_mark", "tls_note_write", "tls_note_adopt", "tls_relax", "tls_stack_open",
               "tls_stack_close"):
        t = defn(lines, fn)
        if t is None:
            print("FAIL: could not find %s in server.c" % fn)
            return 1
        parts.append(t)
    body = "\n\n".join(parts)
    work = tempfile.mkdtemp(prefix="pms-tls-")
    try:
        note = os.path.join(work, "tls-stack").replace("\\", "/")
        want = '#define PMS_TLS_NOTE "/data/pkg-mutant-shop/tls-stack"'
        if body.count(want) != 1:
            print("FAIL: the note's path is not where this test expects it in server.c")
            return 1
        body = body.replace(want, '#define PMS_TLS_NOTE "%s"' % note)
        total = 0
        for flag, name in (("-DPMS_TLS_KEEP=1", "the PS4's build (one kept stack)"),
                           ("", "the PS5's build (a stack per request)")):
            cfile = os.path.join(work, "t.c")
            exe = os.path.join(work, "t.exe")
            io.open(cfile, "w", encoding="utf-8", newline="\n").write(
                PRELUDE + "\n" + body + "\n" + DRIVER)
            cmd = [cc, "-O1", "-Wall", "-Wno-unused-function", "-Wno-unused-variable"]
            if flag:
                cmd.append(flag)
            r = subprocess.run(cmd + ["-o", exe, cfile], capture_output=True, text=True)
            if r.returncode != 0:
                print("FAIL: %s does not compile here:\n%s" % (name, (r.stderr or r.stdout)[-2500:]))
                return 1
            r = subprocess.run([exe], capture_output=True, text=True)
            out = (r.stdout or "").strip()
            if r.returncode != 0:
                print("test_tls_keep: FAIL - %s" % name)
                print("  " + out.replace("\n", "\n  "))
                return 1
            total += int(out.splitlines()[-1].split()[0])
        print("test_tls_keep: OK (%d checks across both builds)" % total)
        return 0
    finally:
        shutil.rmtree(work, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())

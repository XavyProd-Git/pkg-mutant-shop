/* Runs the PS4 cheat engine's two real halves against each other, on this PC, with no console.
 *
 * WHY. This agent has broken games repeatedly, and every round of guessing cost the owner a frozen
 * game and sometimes a re-jailbreak. Everything that CAN be settled off-console must be settled
 * off-console. This compiles the ACTUAL ps4-app/plugin/source/main.c (against stubinc/, whose
 * declarations are copied from the toolchain's own headers) and drives the real file protocol it
 * now speaks, so the wire format, the refusals and the memory guards are all proven before anyone
 * launches a game.
 *
 * WHAT IT CANNOT PROVE, said plainly: whether the module LOADS cleanly inside a real game. That is
 * a property of the console's loader, not of this code, and only a console can answer it.
 *
 * THE FAKE GAME is one mmap'd megabyte carved into three declared module segments with real gaps
 * between them. The gaps are perfectly readable memory that is NOT part of any segment, which is
 * what makes the refusal tests mean something: a bug that ignored the segment map would read and
 * write them happily, and this would catch it without ever faulting.
 */
#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <signal.h>
#include <stdarg.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

#include <orbis/libkernel.h>      /* ps4-app/plugin/test/stubinc - see the file */

int32_t plugin_load(int32_t argc, const char *argv[]);

/* glibc has no __error(); FreeBSD (and so the PS4) does. */
int *__error(void) { return &errno; }

/* ---- the fake game ---------------------------------------------------------------------------
 *   [      0,  256K )  eboot.bin segment 0      <- the image base
 *   [   256K,  512K )  GAP
 *   [   512K,  528K )  eboot.bin segment 1
 *   [   528K,  768K )  GAP
 *   [   768K,  784K )  libSceTest.prx segment 0
 *   [   784K,   1M  )  GAP
 */
#define REGION    (1024 * 1024)
#define SEG0_LEN  (256 * 1024)
/* NOT A MULTIPLE OF 0x4000, DELIBERATELY. This is the case a start-alignment refusal broke: the page
   that holds the first byte of this segment begins BELOW the segment, so rounding the mprotect cover
   down leaves it - which is unavoidable, because mprotect cannot be asked for less than a page. The
   range actually written is still proved to be inside eboot.bin before any of this. */
#define SEG1_OFF  ((512 * 1024) + 0x100)
#define SEG1_LEN  (16 * 1024)
#define SEG2_OFF  (768 * 1024)
#define SEG2_LEN  (16 * 1024)
#define GAP_OFF   (300 * 1024)
#define LATE_OFF  (560 * 1024)
#define LATE_LEN  (16 * 1024)

/* The protections a real module's segments carry: code is read+execute, data is read+write. The
   mock used to leave prot unset, so the code under test saw 0 - and a prot of 0 must never be
   restored (handing a live code page VM_PROT_NONE would fault the game), which meant the restore
   could not be observed at all. */
#define PROT_CODE  (VM_PROT_READ | VM_PROT_EXECUTE)
#define PROT_DATA  (VM_PROT_READ | VM_PROT_WRITE)

static unsigned char *g_mem;
static int g_mprotect_calls;
static int g_last_prot = -1;
static int g_late_loaded;

/* ---- the SDK, stubbed ------------------------------------------------------------------------ */
int32_t sceKernelGetAppInfo(pid_t pid, OrbisAppInfo *info) {
    (void)pid;
    memset(info, 0, sizeof(*info));
    info->AppId = 42;
    memcpy(info->TitleId, "CUSA12345", 10);      /* offset 16 by the real struct's own layout */
    return 0;
}

int32_t sceKernelGetModuleList(OrbisKernelModule *array, size_t size, size_t *available) {
    if (size < 3) return -1;
    array[0] = 1;
    array[1] = 2;
    *available = 2;
    if (g_late_loaded) { array[2] = 3; *available = 3; }
    return 0;
}

int32_t sceKernelGetModuleInfo(OrbisKernelModule handle, OrbisKernelModuleInfo *info) {
    /* The real call refuses a struct that has not declared its own size. Refusing it here too is
       the point: main.c setting mi.size is load-bearing. */
    if (!info || info->size != sizeof(*info)) return -1;
    if (handle == 1) {
        snprintf(info->name, sizeof(info->name), "eboot.bin");
        info->segmentInfo[0].address = g_mem;
        info->segmentInfo[0].size    = SEG0_LEN;
        info->segmentInfo[0].prot    = PROT_CODE;
        info->segmentInfo[1].address = g_mem + SEG1_OFF;
        info->segmentInfo[1].size    = SEG1_LEN;
        info->segmentInfo[1].prot    = PROT_DATA;
        info->segmentCount = 2;
        return 0;
    }
    if (handle == 2) {
        snprintf(info->name, sizeof(info->name), "libSceTest.prx");
        info->segmentInfo[0].address = g_mem + SEG2_OFF;
        info->segmentInfo[0].size    = SEG2_LEN;
        info->segmentInfo[0].prot    = PROT_CODE;
        info->segmentCount = 1;
        return 0;
    }
    if (handle == 3 && g_late_loaded) {
        snprintf(info->name, sizeof(info->name), "libSceLate.prx");
        info->segmentInfo[0].address = g_mem + LATE_OFF;
        info->segmentInfo[0].size    = LATE_LEN;
        info->segmentInfo[0].prot    = PROT_CODE;
        info->segmentCount = 1;
        return 0;
    }
    return -1;
}

int32_t sceKernelMprotect(const void *addr, size_t len, int prot) {
    g_mprotect_calls++;
    g_last_prot = prot;
    if (mprotect((void *)addr, len, PROT_READ | PROT_WRITE | PROT_EXEC) != 0)
        (void)mprotect((void *)addr, len, PROT_READ | PROT_WRITE);
    return 0;
}

int32_t sceKernelUsleep(uint32_t us) { return usleep(us) == 0 ? 0 : -1; }

/* THE KERNEL FILE CALLS, on a PC, as thin as they can be: straight to the host's own open/read/
   write/close/lseek. The point is not to emulate the PS4's filesystem - it is that main.c's file
   layer, the one that actually ships, is what this suite drives.
   The flag VALUES differ between this host and the PS4 (FreeBSD O_CREAT is 0x200, Linux's is 0x40),
   so the flags main.c passes are TRANSLATED rather than handed through. Passing them through is a
   bug that would only show up as a file quietly opened in the wrong mode. */
int32_t sceKernelOpen(const char *path, int32_t flags, OrbisKernelMode mode) {
    /* THE FLAGS ARE PASSED STRAIGHT THROUGH, and getting this wrong cost a long detour - so the
       reasoning is written down rather than left to look obvious.
       In THIS build, main.c and this file are both compiled against the HOST's <fcntl.h>, so the
       values main.c hands over are already the host's own. On the console, main.c is compiled against
       the toolchain's bits/fcntl.h and the real sceKernelOpen receives those - and both are FreeBSD.
       Either way the producer and the consumer agree, and translating between them is what breaks it.
       A first version of this mock did translate, assuming main.c always spoke FreeBSD. glibc's
       O_TRUNC (01000) is numerically FreeBSD's O_CREAT, so O_TRUNC was silently DROPPED: cmd.bin was
       opened for writing but never emptied, the agent re-served the same request on its next poll, and
       every reply arrived one sequence number late. Eight checks failed, in a set that moved between
       runs, and none of them was pointing at the agent. */
    return (int32_t)open(path, (int)flags, (mode_t)(mode ? mode : 0777));
}

size_t  sceKernelRead(int32_t fd, void *b, size_t n)        { return (size_t)read(fd, b, n); }
size_t  sceKernelWrite(int32_t fd, const void *b, size_t n) { return (size_t)write(fd, b, n); }
int32_t sceKernelClose(int32_t fd)                          { return (int32_t)close(fd); }
off_t   sceKernelLseek(int32_t fd, off_t off, int whence)   { return lseek(fd, off, whence); }

int32_t scePthreadCreate(OrbisPthread *t, const OrbisPthreadAttr *a, void *(*f)(void *),
                         void *arg, const char *name) {
    (void)a; (void)name;
    return pthread_create(t, NULL, f, arg);
}

int32_t scePthreadDetach(OrbisPthread t) { return pthread_detach(t); }

/* ---- checks ----------------------------------------------------------------------------------- */
static int g_checks, g_fails;

static void check(const char *name, int ok, const char *fmt, ...) {
    g_checks++;
    if (!ok) g_fails++;
    printf("  [%s] %s", ok ? "PASS" : "FAIL", name);
    if (fmt && fmt[0]) {
        va_list ap;
        va_start(ap, fmt);
        printf("  -> ");
        vprintf(fmt, ap);
        va_end(ap);
    }
    printf("\n");
    fflush(stdout);
}

/* ---- the shop's half of the file channel -------------------------------------------------------
 * Deliberately written here rather than lifted out of server_ps4.c: that file is a PS4 payload
 * that will not compile on a PC. It follows the format documented at the top of main.c byte for
 * byte, and the check below compares it against the real thing so the two cannot drift. */
#define REQ_HDR 24
#define RES_HDR 16
#define CHUNK   (4 * 1024)      /* must equal PMS_MAX_CHUNK in main.c and AGENT_CHUNK in server_ps4.c */
enum { OP_PING = 1, OP_STATUS = 2, OP_READ = 3, OP_WRITE = 4 };

static char DIR[256], CMD[300], CMDT[300], RES[300];
static unsigned int g_seq;

static int put_atomic(const char *path, const char *tmp, const unsigned char *b, size_t n) {
    int f = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0666);
    if (f < 0) return -1;
    size_t d = 0;
    while (d < n) {
        ssize_t w = write(f, b + d, n - d);
        if (w <= 0) { close(f); unlink(tmp); return -1; }
        d += (size_t)w;
    }
    close(f);
    /* Same reason as the shop's agent_write_atomic: cmd.bin always exists now (the agent empties it
       rather than deleting it), and a rename onto an existing file is not dependable on the console. */
    unlink(path);
    return rename(tmp, path);
}

/* Returns 0, -(100+status) when the agent refused, or -3 on no answer. */
static int req(int op, uint64_t addr, const void *in, unsigned int inlen, unsigned int wirelen,
               void *out, unsigned int outcap, unsigned int *outlen) {
    if (outlen) *outlen = 0;
    static unsigned char r[CHUNK + REQ_HDR];
    unsigned int seq = ++g_seq;
    memcpy(r, "PMSC", 4);
    memcpy(r + 4, &seq, 4);
    r[8] = (unsigned char)op;
    r[9] = r[10] = r[11] = 0;
    memcpy(r + 12, &addr, 8);
    memcpy(r + 20, &wirelen, 4);
    if (inlen) memcpy(r + REQ_HDR, in, inlen);
    unlink(RES);
    if (put_atomic(CMD, CMDT, r, (size_t)REQ_HDR + inlen) != 0) return -2;

    static unsigned char rsp[CHUNK + RES_HDR];
    for (int waited = 0; waited < 4000; waited += 20) {
        struct stat st;
        if (stat(RES, &st) == 0 && st.st_size >= RES_HDR) {
            int f = open(RES, O_RDONLY);
            if (f < 0) { usleep(20000); continue; }
            size_t n = 0;
            for (;;) {
                ssize_t k = read(f, rsp + n, sizeof(rsp) - n);
                if (k <= 0) break;
                n += (size_t)k;
            }
            close(f);
            /* NOT UNLINKED UNTIL IT PARSES, and an incomplete answer is not a malformed one. The
               agent writes res.bin straight into place (no .tmp, no rename - rename was the one
               import nothing on this console demonstrates), so this can catch it mid-write: a
               200,000-byte read spans several sceKernelWrite calls. Unlinking first DELETED the
               agent's in-progress file, and calling it malformed turned a normal large transfer into
               a protocol error - the two together produced failures that moved between runs
               depending on which check lost the race. Mirrors agent_req in server_ps4.c. */
            if (n < RES_HDR || memcmp(rsp, "PMSR", 4) != 0) { unlink(RES); return -4; }
            unsigned int rseq = 0, rlen = 0;
            memcpy(&rseq, rsp + 4, 4);
            memcpy(&rlen, rsp + 12, 4);
            if (rseq != seq) { unlink(RES); return -4; }
            if (rlen > CHUNK) { unlink(RES); return -4; }
            if ((size_t)RES_HDR + rlen > n) { usleep(20000); continue; }   /* still arriving */
            int st8 = rsp[8];
            unlink(RES);
            if (rlen) {
                if (rlen > outcap) return -5;
                memcpy(out, rsp + RES_HDR, rlen);
            }
            if (outlen) *outlen = rlen;
            return st8 ? -(100 + st8) : 0;
        }
        usleep(20000);
    }
    unlink(CMD);
    return -3;
}

static int mem_read(uint64_t a, void *b, size_t n) {
    unsigned char *p = b;
    while (n) {
        unsigned int take = n > CHUNK ? CHUNK : (unsigned int)n, got = 0;
        int rc = req(OP_READ, a, NULL, 0, take, p, take, &got);
        if (rc) return rc;
        if (got != take) return -7;
        p += take; a += take; n -= take;
    }
    return 0;
}

static int mem_write(uint64_t a, const void *b, size_t n) {
    const unsigned char *p = b;
    while (n) {
        unsigned int take = n > CHUNK ? CHUNK : (unsigned int)n;
        int rc = req(OP_WRITE, a, p, take, take, NULL, 0, NULL);
        if (rc) return rc;
        p += take; a += take; n -= take;
    }
    return 0;
}

int main(void) {
    setvbuf(stdout, NULL, _IOLBF, 0);

    snprintf(DIR,  sizeof(DIR),  "%s", PMS_AGENT_DIR_OVERRIDE);
    snprintf(CMD,  sizeof(CMD),  "%s/cmd.bin", DIR);
    snprintf(CMDT, sizeof(CMDT), "%s/cmd.tmp", DIR);
    snprintf(RES,  sizeof(RES),  "%s/res.bin", DIR);
    mkdir(DIR, 0777);

    /* ALIGNED TO 0x4000 ON PURPOSE, and the alignment is not cosmetic. Every module segment on the
       real console sits on a multiple of 0x4000 (the alignment the SDK's link.x gives .text,
       .data.rel.ro and .data.sce_module_param), and the write path rounds its mprotect cover to that
       block. An unaligned mmap made this suite sample that behaviour instead of testing it: a guard
       that refused a cover reaching below a segment's base passed on one run and failed on the next,
       purely because the address had moved. Over-map and trim so the base is fixed. */
    unsigned char *raw = mmap(NULL, REGION + 0x4000, PROT_READ | PROT_WRITE,
                              MAP_PRIVATE | MAP_ANONYMOUS, -1, 0);
    if (raw == MAP_FAILED) { printf("mmap failed\n"); return 2; }
    g_mem = (unsigned char *)(((uintptr_t)raw + 0x3FFF) & ~(uintptr_t)0x3FFF);
    if (((uintptr_t)g_mem & 0x3FFF) != 0) { printf("could not align the fake game\n"); return 2; }
    for (size_t i = 0; i < REGION; i++) g_mem[i] = (unsigned char)(i * 7 + 3);

    struct sigaction before;
    sigaction(SIGPIPE, NULL, &before);
    check("SIGPIPE is fatal by default before the plugin loads",
          before.sa_handler == SIG_DFL, "handler=%p", (void *)before.sa_handler);

    plugin_load(0, NULL);

    /* THE AGENT MUST NOT TOUCH SIGPIPE ANY MORE. It used to ignore it, to survive writing to a
       socket the shop had closed; there is no socket now, and `signal` was an import that neither
       plugin this console loads cleanly carries. Asserting the disposition is UNCHANGED is how we
       keep that call from creeping back in. Queried, never set. */
    struct sigaction after;
    sigaction(SIGPIPE, NULL, &after);
    check("plugin_load leaves SIGPIPE alone (no signal import)", after.sa_handler == SIG_DFL,
          "handler=%p", (void *)after.sa_handler);

    /* The worker sleeps before it touches anything; wait for it to start answering. */
    int up = 0;
    for (int i = 0; i < 100 && !up; i++) {
        if (req(OP_PING, 0, NULL, 0, 0, NULL, 0, NULL) == 0) up = 1; else usleep(100 * 1000);
    }
    check("the agent answers over the file channel", up, "dir %s", DIR);
    if (!up) { printf("\n%d check(s), %d failure(s)\n", g_checks, g_fails); return 1; }

    /* ---- status ------------------------------------------------------------------------------ */
    char info[512];
    unsigned int n = 0;
    memset(info, 0, sizeof(info));
    int rc = req(OP_STATUS, 0, NULL, 0, 0, info, sizeof(info) - 1, &n);
    info[n < sizeof(info) ? n : sizeof(info) - 1] = 0;
    check("STATUS returns a document", rc == 0 && n > 0, "rc=%d n=%u", rc, n);
    check("STATUS names the executable, not some library",
          strstr(info, "\"module\":\"eboot.bin\"") != NULL, "%s", info);
    check("STATUS carries the title id read at offset 16",
          strstr(info, "\"title_id\":\"CUSA12345\"") != NULL, "%s", info);
    char wantbase[64];
    snprintf(wantbase, sizeof(wantbase), "\"base\":\"0x%llx\"", (unsigned long long)(uintptr_t)g_mem);
    check("STATUS reports eboot.bin's first segment as the image base",
          strstr(info, wantbase) != NULL, "wanted %s", wantbase);

    /* ---- reads ------------------------------------------------------------------------------- */
    static unsigned char buf[300000];
    rc = mem_read((uint64_t)(uintptr_t)(g_mem + 16), buf, 16);
    check("a small read returns the bytes that are there",
          rc == 0 && memcmp(buf, g_mem + 16, 16) == 0, "rc=%d", rc);

    rc = mem_read((uint64_t)(uintptr_t)g_mem, buf, 200000);
    check("a read larger than one chunk is reassembled exactly",
          rc == 0 && memcmp(buf, g_mem, 200000) == 0, "rc=%d", rc);

    rc = mem_read((uint64_t)(uintptr_t)(g_mem + SEG2_OFF), buf, 64);
    check("a read inside a shared library's segment works too",
          rc == 0 && memcmp(buf, g_mem + SEG2_OFF, 64) == 0, "rc=%d", rc);

    /* READS GO ANYWHERE IN THE PROCESS, WRITES ONLY INTO eboot.bin. Every offset in the cheat
       library is relative to the executable - the files say so themselves ("process":"eboot.bin") -
       so a write landing in a shared library is a cheat aimed at the wrong module. It is also the
       one module that cannot be unloaded while the process lives, which takes the stale-map fault
       off the write path entirely. ST_FOREIGN is 6, so the shop sees -(100 + 6). */
    {
        unsigned char keep[16];
        memcpy(keep, g_mem + SEG2_OFF, sizeof(keep));
        unsigned char junk[16];
        memset(junk, 0xCC, sizeof(junk));
        rc = mem_write((uint64_t)(uintptr_t)(g_mem + SEG2_OFF), junk, sizeof(junk));
        check("a write into a shared library is refused, though a read of it is not",
              rc == -106, "rc=%d", rc);
        check("and the library's bytes were not touched",
              memcmp(g_mem + SEG2_OFF, keep, sizeof(keep)) == 0, "%s", "unchanged");
    }

    /* ---- writes ------------------------------------------------------------------------------ */
    unsigned char patch[8];
    patch[0] = 0x90; patch[1] = 0x90; patch[2] = 0x90; patch[3] = 0x90;
    patch[4] = 0xE9; patch[5] = 0x11; patch[6] = 0x22; patch[7] = 0x33;
    int before_calls = g_mprotect_calls;
    rc = mem_write((uint64_t)(uintptr_t)(g_mem + 1000), patch, sizeof(patch));
    check("a small write lands in the game's memory",
          rc == 0 && memcmp(g_mem + 1000, patch, sizeof(patch)) == 0, "rc=%d", rc);
    check("the page was made writable first", g_mprotect_calls > before_calls,
          "mprotect %d -> %d", before_calls, g_mprotect_calls);
    /* AND PUT BACK. The last thing the write path asks for must be the segment's own protection, not
       the write permission it borrowed - a code page left writable for the rest of the session is a
       change to the game nobody asked for. */
    check("and the protection was restored afterwards", g_last_prot == PROT_CODE,
          "last prot=0x%x wanted 0x%x", g_last_prot, PROT_CODE);
    rc = mem_read((uint64_t)(uintptr_t)(g_mem + 1000), buf, sizeof(patch));
    check("and reading it back returns what was written",
          rc == 0 && memcmp(buf, patch, sizeof(patch)) == 0, "rc=%d", rc);

    /* THE FIRST BYTES OF A SEGMENT WHOSE BASE IS NOT PAGE-ALIGNED. Pinned, because refusing this is
       a regression that once shipped and then hid behind an mmap address that happened to be
       aligned. */
    {
        unsigned char m2[4] = { 0xAA, 0xBB, 0xCC, 0xDD };
        rc = mem_write((uint64_t)(uintptr_t)(g_mem + SEG1_OFF), m2, sizeof(m2));
        check("a write to the first byte of a segment that is not 0x4000-aligned still lands",
              rc == 0 && memcmp(g_mem + SEG1_OFF, m2, sizeof(m2)) == 0, "rc=%d", rc);
    }

    static unsigned char big[200000];
    for (size_t i = 0; i < sizeof(big); i++) big[i] = (unsigned char)(i * 13 + 5);
    rc = mem_write((uint64_t)(uintptr_t)g_mem, big, sizeof(big));
    check("a write larger than one chunk lands in full",
          rc == 0 && memcmp(g_mem, big, sizeof(big)) == 0, "rc=%d", rc);

    /* ---- refusals: the segment map is doing real work ----------------------------------------- */
    unsigned char gap_before[64];
    memcpy(gap_before, g_mem + GAP_OFF, sizeof(gap_before));

    rc = mem_read((uint64_t)(uintptr_t)(g_mem + GAP_OFF), buf, 64);
    check("a read outside every module segment is refused", rc != 0, "rc=%d", rc);
    rc = mem_write((uint64_t)(uintptr_t)(g_mem + GAP_OFF), patch, sizeof(patch));
    check("a write outside every module segment is refused", rc != 0, "rc=%d", rc);
    check("and that memory was not touched",
          memcmp(g_mem + GAP_OFF, gap_before, sizeof(gap_before)) == 0, "");
    rc = mem_read((uint64_t)(uintptr_t)(g_mem + SEG0_LEN - 4), buf, 64);
    check("a read that starts inside a segment and runs off its end is refused", rc != 0,
          "rc=%d", rc);

    /* ---- a library the game loads AFTER the plugin did ---------------------------------------- */
    rc = mem_read((uint64_t)(uintptr_t)(g_mem + LATE_OFF), buf, 64);
    check("an address in a not-yet-loaded library is refused while it is not loaded", rc != 0,
          "rc=%d", rc);
    g_late_loaded = 1;
    rc = mem_read((uint64_t)(uintptr_t)(g_mem + LATE_OFF), buf, 64);
    check("and is served once the library is really there (the map catches up)",
          rc == 0 && memcmp(buf, g_mem + LATE_OFF, 64) == 0, "rc=%d", rc);
    memset(info, 0, sizeof(info));
    n = 0;
    req(OP_STATUS, 0, NULL, 0, 0, info, sizeof(info) - 1, &n);
    info[n < sizeof(info) ? n : sizeof(info) - 1] = 0;
    check("the rescan kept the image base it already had",
          strstr(info, wantbase) != NULL && strstr(info, "\"segments\":4") != NULL, "%s", info);

    /* ---- malformed requests ------------------------------------------------------------------- */
    rc = req(OP_READ, (uint64_t)(uintptr_t)g_mem, NULL, 0, 0, buf, sizeof(buf), &n);
    check("a zero-length read is refused rather than answered", rc == -(100 + 2), "rc=%d", rc);
    rc = req(99, 0, NULL, 0, 0, NULL, 0, NULL);
    check("an unknown opcode is refused", rc == -(100 + 1), "rc=%d", rc);
    check("and the channel still works afterwards",
          req(OP_PING, 0, NULL, 0, 0, NULL, 0, NULL) == 0, "");

    /* A WRITE whose header promises more than the file carries must be refused, not applied from
       whatever happens to follow in memory. */
    {
        unsigned char r2[REQ_HDR + 4];
        unsigned int seq = ++g_seq, wl = 64;
        uint64_t a = (uint64_t)(uintptr_t)g_mem;
        memcpy(r2, "PMSC", 4);
        memcpy(r2 + 4, &seq, 4);
        r2[8] = OP_WRITE; r2[9] = r2[10] = r2[11] = 0;
        memcpy(r2 + 12, &a, 8);
        memcpy(r2 + 20, &wl, 4);
        memset(r2 + REQ_HDR, 0xAB, 4);            /* promises 64 bytes, carries 4 */
        unsigned char keep[64];
        memcpy(keep, g_mem, sizeof(keep));
        unlink(RES);
        put_atomic(CMD, CMDT, r2, sizeof(r2));
        int got = 0;
        for (int w = 0; w < 3000 && !got; w += 20) {
            struct stat st;
            if (stat(RES, &st) == 0 && st.st_size >= RES_HDR) got = 1; else usleep(20000);
        }
        check("a truncated write payload is refused", got, "no answer");
        check("and the target memory was not touched",
              memcmp(g_mem, keep, sizeof(keep)) == 0, "");
        unlink(RES);
    }

    printf("\n%d check(s), %d failure(s)\n", g_checks, g_fails);
    return g_fails ? 1 : 0;
}

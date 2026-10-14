/* test_cheat_core.c - the real engine, compiled on the PC, with memory it cannot corrupt.
 *
 * WHY THIS EXISTS. Every claim this project has made about the cheat engine has been checked by
 * reading it. Reading missed a `section` parsed with strtol against a quoted value (a check that
 * could never fire), and before that it missed a mod applied half-way - which crashed Dark Souls II
 * the moment the owner hit an enemy. So the engine is compiled here, given a fake game whose memory
 * lives in this process, and asked to prove what it does.
 *
 * It is the SHIPPED code: cheat_core.h is generated from ps5-app/onconsole/server.c, so a pass here
 * is a statement about both consoles. mem_read/mem_write are the only things faked - which is
 * exactly the seam each console fills in for itself.
 *
 * Run it through tools/test_cheat_core.py (which compiles it and is what CI/the build gate calls).
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdint.h>
#include <stddef.h>

#include <fcntl.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <dirent.h>
#include <pthread.h>          /* the signature search runs in a thread */
#include <unistd.h>           /* usleep, for waiting on it */
#include <time.h>

typedef int pid_t_shim;
#define pid_t pid_t_shim

/* The engine reads and writes files (the cheat library, patch undo logs). None of that is what this
   test is about, but it has to compile, so the few names the server would have provided are given
   here. mkdir takes one argument on this host's libc and two on a console's. */
static int mkdir_shim(const char *p, int mode) { (void)mode; return mkdir(p); }
#define mkdir(p, m) mkdir_shim((p), (m))

static void notifyf(const char *fmt, ...) { (void)fmt; }
static void notify(const char *s) { (void)s; }

static char *slurp(const char *path, long *len_out) {
    FILE *f = fopen(path, "rb");
    if (!f) return NULL;
    fseek(f, 0, SEEK_END);
    long n = ftell(f);
    fseek(f, 0, SEEK_SET);
    char *b = (char *)malloc((size_t)(n > 0 ? n : 0) + 1);
    if (!b) { fclose(f); return NULL; }
    size_t got = n > 0 ? fread(b, 1, (size_t)n, f) : 0;
    fclose(f);
    b[got] = 0;
    if (len_out) *len_out = (long)got;
    return b;
}

/* ---- the fake game -------------------------------------------------------------------------
 * A flat window of bytes at FAKE_BASE. Reads and writes outside it fail, which is how "the engine
 * could not read that place" is produced on purpose. Every write is counted and logged so a test
 * can assert that NOTHING was written - the atomicity claim is about absence, and absence is only
 * checkable if writes are observable. */
#define FAKE_BASE  0x400000L
/* Big enough for the REAL offsets in the shipped library: Dark Souls II's master code writes
   at +0x2077800 and its "1 hit kill" cave sits at +0x2077868, so a 64 KB window could not hold
   the one test that matters most. Heap, because 35 MB of .bss is not worth it. */
#define FAKE_SIZE  0x2200000
static unsigned char *g_game;
static int g_reads_fail = 0;            /* 1 = every read fails, for the unreadable case */
static int g_write_calls = 0;
static long g_write_addr[64];
static int g_write_deny_at = -1;        /* an address whose write fails, to test pass two */

static int mem_read(pid_t pid, intptr_t addr, void *out, size_t len) {
    (void)pid;
    if (g_reads_fail) return -1;
    if (addr < FAKE_BASE || addr + (intptr_t)len > FAKE_BASE + FAKE_SIZE) return -1;
    memcpy(out, g_game + (addr - FAKE_BASE), len);
    return 0;
}

static int mem_write(pid_t pid, intptr_t addr, const void *in, size_t len) {
    (void)pid;
    if (g_write_calls < 64) g_write_addr[g_write_calls] = (long)addr;
    g_write_calls++;
    if (g_write_deny_at >= 0 && addr == (intptr_t)g_write_deny_at) return -1;
    if (addr < FAKE_BASE || addr + (intptr_t)len > FAKE_BASE + FAKE_SIZE) return -1;
    memcpy(g_game + (addr - FAKE_BASE), in, len);
    return 0;
}

static int running_game(char *tid, size_t tsz, pid_t *pid, intptr_t *base) {
    if (tid && tsz) snprintf(tid, tsz, "CUSA00000");
    if (pid) *pid = 1;
    if (base) *base = FAKE_BASE;
    return 1;
}

static void json_escape(const char *in, char *out, size_t osz) {
    size_t o = 0;
    for (size_t i = 0; in && in[i] && o + 2 < osz; i++) {
        if (in[i] == '"' || in[i] == '\\') out[o++] = '\\';
        out[o++] = in[i];
    }
    if (osz) out[o < osz ? o : osz - 1] = 0;
}

static long long now_ms(void) { return 0; }

#define CHEAT_ROOT       "./_t_cheats"
#define CHEAT_JSON_DIR   CHEAT_ROOT "/json"
#define CHEAT_SHN_DIR    CHEAT_ROOT "/shn"
#define CHEAT_PATCH_DIR  CHEAT_ROOT "/patches"
#define CHEAT_MC4_DIR    CHEAT_ROOT "/mc4"
#define CHEAT_INBOX_DIR  CHEAT_ROOT "/incoming"

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

#include "../ps4-app/onconsole/cheat_core.h"

/* ---- the harness ---------------------------------------------------------------------------- */
static int g_fail = 0, g_ran = 0;
static const char *g_root = ".";   /* argv[1]: the repo, so the real cheat file can be read */

static void ok(int cond, const char *what) {
    g_ran++;
    if (!cond) { g_fail++; printf("  FAIL  %s\n", what); }
}

static void okf(int cond, const char *what, const char *extra) {
    g_ran++;
    if (!cond) { g_fail++; printf("  FAIL  %s   [%s]\n", what, extra); }
}

static void reset(void) {
    memset(g_game, 0xCC, FAKE_SIZE);
    g_reads_fail = 0;
    g_write_calls = 0;
    g_write_deny_at = -1;
    memset(g_write_addr, 0, sizeof(g_write_addr));
}

static void put(long off, const char *hex) {
    unsigned char b[64];
    int n = hex2bytes(hex, b, sizeof(b));
    if (n > 0) memcpy(g_game + off, b, (size_t)n);
}

/* apply mod `index` of `doc`, returning rc and filling detail */
static int apply(const char *doc, int index, int want_on, int force, char *detail, size_t dsz) {
    const char *from = mods_array_start(doc);
    for (int m = 0; from; m++) {
        const char *end = NULL;
        const char *blk = next_mod_block(from, &end);
        if (!blk) break;
        from = end;
        if (m == index)
            return cheat_apply_blk(doc, 0, blk, end, index, want_on, 1, FAKE_BASE, force, 0,
                                   detail, dsz);
    }
    return -99;
}

/* ============================================================================================
 * 1. section is parsed, and it is a STRING in every file we ship
 * ========================================================================================== */
static const char *DOC_SECTION =
"{\"name\":\"t\",\"id\":\"CUSA00000\",\"version\":\"01.00\",\"process\":\"eboot.bin\",\"mods\":["
"{\"name\":\"other module\",\"memory\":[{\"section\":\"11\",\"offset\":\"1000\",\"on\":\"90909090\",\"off\":\"41890470\"}]},"
"{\"name\":\"unquoted section\",\"memory\":[{\"section\":14,\"offset\":\"1010\",\"on\":\"9090\",\"off\":\"7405\"}]},"
"{\"name\":\"no section\",\"memory\":[{\"offset\":\"1020\",\"on\":\"9090\",\"off\":\"7405\"}]}]}";

static void test_section_parse(void) {
    printf("section is read off the entry (quoted and bare)\n");
    const char *from = mods_array_start(DOC_SECTION);
    int want[3] = {11, 14, 0};
    for (int m = 0; m < 3; m++) {
        const char *end = NULL;
        const char *blk = next_mod_block(from, &end);
        ok(blk != NULL, "mod block found");
        if (!blk) return;
        from = end;
        cheat_entry_t es[4];
        int n = parse_mod_entries(blk, end, es, 4);
        char msg[120];
        snprintf(msg, sizeof(msg), "mod %d: one entry, section %d (got %d)", m, want[m],
                 n > 0 ? es[0].section : -1);
        okf(n == 1 && es[0].section == want[m], msg, "parse_mod_entries");
    }
}

/* ============================================================================================
 * 2. the refusal says WHICH of the three things went wrong
 * ========================================================================================== */
static void test_refusal_reasons(void) {
    printf("a refusal names its cause\n");
    char d[256];

    /* (a) A SECTIONED ENTRY IS REFUSED FOR ITS SECTION, before its bytes are even consulted. 306
       entries in 103 shipped files carry another module's index and 305 of those addresses pass
       ADDR_OK - so without this the engine computes base + offset and writes there when the bytes
       happen to match. The message has claimed since 3.82.0 that these are declined. */
    reset();
    put(0x1000, "DEADBEEF");                       /* neither 90909090 nor 41890470 */
    int rc = apply(DOC_SECTION, 0, 1, 0, d, sizeof(d));
    okf(rc == -4, "section: refused with -4", d);
    okf(strstr(d, "section=1") != NULL, "section: counted as a module problem", d);
    okf(strstr(d, "mismatch=0") != NULL, "section: NOT blamed on the bytes", d);
    okf(g_write_calls == 0, "section: nothing was written", d);

    char msg[400];
    cheat_rc_message(rc, d, 1, msg, sizeof(msg));
    okf(strstr(msg, "different part of the game") != NULL,
        "section: the sentence blames the module, not the game version", msg);

    /* ...AND EVEN WHEN THE BYTES WOULD HAVE MATCHED. This is the case that used to write into the
       wrong place: put the documented `off` bytes at base + offset and the old gate was satisfied. */
    reset();
    put(0x1000, "41890470");                       /* exactly what the file documents */
    rc = apply(DOC_SECTION, 0, 1, 0, d, sizeof(d));
    okf(rc == -4, "section: still refused when the bytes DO match", d);
    okf(g_write_calls == 0, "section: and still nothing written", d);
    ok(memcmp(g_game + 0x1000, "\x41\x89\x04\x70", 4) == 0, "section: the game is untouched");
    /* and force cannot talk it into writing another module's offset into this one */
    rc = apply(DOC_SECTION, 0, 1, 1, d, sizeof(d));
    okf(g_write_calls == 0, "section: force does not override it either", d);

    /* a mod with NO section in the same document still works, so this is not a blanket refusal */
    reset();
    put(0x1020, "7405");
    rc = apply(DOC_SECTION, 2, 1, 0, d, sizeof(d));
    okf(rc > 0, "section: an entry without one still applies", d);

    /* (b) the engine cannot read at all -> unreadable, and a different sentence */
    reset();
    g_reads_fail = 1;
    rc = apply(DOC_SECTION, 2, 1, 0, d, sizeof(d));
    okf(rc == -4, "unreadable: refused with -4", d);
    okf(strstr(d, "unreadable=1") != NULL, "unreadable: counted as unreadable", d);
    okf(strstr(d, "mismatch=0") != NULL, "unreadable: not counted as a version mismatch", d);
    cheat_rc_message(rc, d, 1, msg, sizeof(msg));
    okf(strstr(msg, "could not read") != NULL, "unreadable: the sentence says so", msg);

    /* (c) the address is not a user address -> bad_addr */
    static const char *DOC_BAD =
    "{\"mods\":[{\"name\":\"far\",\"memory\":[{\"offset\":\"7FFFFFFFFFFF\",\"on\":\"90\",\"off\":\"41\"}]}]}";
    reset();
    rc = apply(DOC_BAD, 0, 1, 0, d, sizeof(d));
    okf(rc == -4, "bad address: refused with -4", d);
    okf(strstr(d, "bad_addr=1") != NULL, "bad address: counted as an address problem", d);
    cheat_rc_message(rc, d, 1, msg, sizeof(msg));
    okf(strstr(msg, "does not have memory") != NULL, "bad address: the sentence says so", msg);
}

/* ============================================================================================
 * 3. ALL OF IT OR NONE OF IT - the Dark Souls II crash
 *    Two entries, one of them a 36-byte code cave whose "off" state is zeros. If the cave cannot
 *    be written, the jump must not be either.
 * ========================================================================================== */
static const char *DOC_HOOK =
"{\"mods\":[{\"name\":\"1 hit kill\",\"memory\":["
"{\"offset\":\"2000\",\"on\":\"41898770010000909090909090909090904189877001000090909090909090909090\",\"off\":\"00000000000000000000000000000000000000000000000000000000000000000000\"},"
"{\"offset\":\"3000\",\"on\":\"E9FBEFFFFF9090\",\"off\":\"41898770010000\"}]}]}";

static void test_whole_or_nothing(void) {
    printf("a mod is applied whole or not at all\n");
    char d[256];

    /* the hook site holds its documented original code, but the cave does NOT hold zeros */
    reset();
    put(0x2000, "1122334455667788990011223344556677889900112233445566778899001122");   /* cave is not empty -> unwritable */
    put(0x3000, "41898770010000");                       /* the jump site IS ready */
    int rc = apply(DOC_HOOK, 0, 1, 0, d, sizeof(d));
    okf(rc == -4, "half a hook: refused", d);
    okf(strstr(d, "mismatch=1") != NULL,
        "half a hook: refused by the byte gate, not by a malformed entry", d);
    okf(g_write_calls == 0, "half a hook: the jump was NOT written on its own", d);
    ok(memcmp(g_game + 0x3000, "\x41\x89\x87\x70\x01\x00\x00", 7) == 0,
       "half a hook: the game's original code is untouched");

    /* both entries ready -> both written, and the mod reports on */
    reset();
    memset(g_game + 0x2000, 0, 34);
    put(0x3000, "41898770010000");
    rc = apply(DOC_HOOK, 0, 1, 0, d, sizeof(d));
    okf(rc > 0, "whole hook: applied", d);
    okf(g_write_calls == 2, "whole hook: both entries written", d);
    ok(g_game[0x3000] == 0xE9, "whole hook: the jump is in place");
    ok(g_game[0x2000] == 0x41, "whole hook: the cave is in place");

    /* force still goes ahead - that is what force is for */
    reset();
    put(0x2000, "1122334455667788990011223344556677889900112233445566778899001122");
    put(0x3000, "41898770010000");
    rc = apply(DOC_HOOK, 0, 1, 1, d, sizeof(d));
    okf(rc > 0 && g_write_calls == 2, "force: writes anyway", d);
}

/* ============================================================================================
 * 4. the ordinary paths still behave - a gate that refuses everything would pass tests 2 and 3
 * ========================================================================================== */
static void test_normal_paths(void) {
    printf("the ordinary cases are unchanged\n");
    char d[256];
    static const char *DOC =
    "{\"mods\":[{\"name\":\"inf ammo\",\"memory\":[{\"offset\":\"1100\",\"on\":\"90909090\",\"off\":\"41890470\"}]}]}";

    reset();
    put(0x1100, "41890470");
    int rc = apply(DOC, 0, 1, 0, d, sizeof(d));
    okf(rc > 0, "off -> on: applied", d);
    ok(memcmp(g_game + 0x1100, "\x90\x90\x90\x90", 4) == 0, "off -> on: the bytes are there");

    rc = apply(DOC, 0, 1, 0, d, sizeof(d));
    okf(rc == 0, "already on: reported as nothing to do", d);

    rc = apply(DOC, 0, 0, 0, d, sizeof(d));
    okf(rc > 0, "on -> off: reverted", d);
    ok(memcmp(g_game + 0x1100, "\x41\x89\x04\x70", 4) == 0, "on -> off: the original code is back");

    const char *from = mods_array_start(DOC);
    const char *end = NULL;
    const char *blk = next_mod_block(from, &end);
    ok(strcmp(cheat_mod_state_blk(DOC, blk, end, 1, FAKE_BASE, 0), "off") == 0,
       "state reads back as off");
    put(0x1100, "90909090");
    ok(strcmp(cheat_mod_state_blk(DOC, blk, end, 1, FAKE_BASE, 0), "on") == 0,
       "state reads back as on");
    put(0x1100, "DEADBEEF");
    ok(strcmp(cheat_mod_state_blk(DOC, blk, end, 1, FAKE_BASE, 0), "unknown") == 0,
       "state of a build that does not match reads as unknown");
}

/* ============================================================================================
 * 5. a write that fails in pass two is still reported, not swallowed
 * ========================================================================================== */
static void test_write_failure(void) {
    printf("a failed write is reported\n");
    char d[256];
    static const char *DOC =
    "{\"mods\":[{\"name\":\"two places\",\"memory\":["
    "{\"offset\":\"1200\",\"on\":\"9090\",\"off\":\"7405\"},"
    "{\"offset\":\"1300\",\"on\":\"9090\",\"off\":\"7405\"}]}]}";
    reset();
    put(0x1200, "7405");
    put(0x1300, "7405");
    g_write_deny_at = FAKE_BASE + 0x1300;
    int rc = apply(DOC, 0, 1, 0, d, sizeof(d));
    okf(rc < 0, "a write that fails makes the apply fail", d);
    okf(strstr(d, "written=1") != NULL && strstr(d, "failed=1") != NULL,
        "the detail says one landed and one did not", d);
    char msg[400];
    cheat_rc_message(rc, d, 1, msg, sizeof(msg));
    okf(strstr(msg, "Only part of this") != NULL, "the sentence says it is half applied", msg);
}

/* ============================================================================================
 * 6. THE MASTER CODE - and the actual Dark Souls II file, not a fixture
 *
 * The crash the owner reported is reproduced here as an ORDERING and PRESENCE question, which is
 * what it was: the cheat's cave reads a scratch qword that only the master's first instruction ever
 * writes. So: the master must be in place, and it must be in place BEFORE the jump that reaches it.
 * ========================================================================================== */
/* A PLAIN TRAMPOLINE, which is what the 20 master blocks in the library actually are: a cave holding
 * a stolen instruction and a jump back, and a hook that jumps into the cave. Deliberately with no
 * RIP-relative operand - Dark Souls II's real one addresses image offset -0x3FC000 and is refused,
 * which is tested on the real file further down and would only get in the way here.
 *
 *   cave  +0x5000  19 bytes  90 90 90 90 90 | 8B 83 70 01 00 00 | 90 90 90 | E9 <back>
 *   hook  +0x1000   6 bytes  E9 FB3F0000 90      -> +0x5000, inside the cave above
 * and the two mods sit OUTSIDE the cave, because this group is about ordering and idempotence.
 */
static const char *DOC_MASTER =
"{\"name\":\"t\",\"master\":{\"challenged\":\"yes\",\"memory\":["
"{\"offset\":\"5000\",\"on\":\"90909090908B8370010000909090E9F6AFFFFF\"},"
"{\"offset\":\"1000\",\"on\":\"E9FB3F000090\"}]},"
"\"mods\":["
"{\"name\":\"needs the master\",\"memory\":[{\"offset\":\"4200\",\"on\":\"9090\",\"off\":\"7405\"}]},"
"{\"name\":\"also needs it\",\"memory\":[{\"offset\":\"4300\",\"on\":\"9090\",\"off\":\"7405\"}]}]}";

static void test_master(void) {
    printf("the master code is installed before the mod that needs it\n");
    char d[256], why[160];

    /* the game as the file describes it: an empty cave, and the hook site holding the instruction
       the cave re-executes. Both are what the new gate requires. */
    reset();
    memset(g_game + 0x5000, 0, 19);
    put(0x1000, "8B8370010000");
    put(0x4200, "7405");
    put(0x4300, "7405");
    int rc = apply(DOC_MASTER, 0, 1, 0, d, sizeof(d));
    okf(rc > 0, "master: the mod applied", d);
    okf(strstr(d, "master=2") != NULL, "master: both master entries were written, and it says so", d);
    ok(g_game[0x5000] == 0x90 && memcmp(g_game + 0x5005, "\x8B\x83\x70\x01\x00\x00", 6) == 0,
       "master: the cave holds its routine");
    ok(g_game[0x1000] == 0xE9, "master: the hook is in place");
    ok(memcmp(g_game + 0x4200, "\x90\x90", 2) == 0, "master: the mod's bytes are in place too");
    /* ORDER. Both master writes must precede the mod's. */
    okf(g_write_calls == 3, "master: three writes in total", d);
    ok(g_write_addr[0] == FAKE_BASE + 0x5000 && g_write_addr[1] == FAKE_BASE + 0x1000
       && g_write_addr[2] == FAKE_BASE + 0x4200,
       "master: written first, mod second - a jump never reaches an empty cave");

    /* IDEMPOTENT. A second cheat in the same file must not rewrite the master. */
    g_write_calls = 0;
    rc = apply(DOC_MASTER, 1, 1, 0, d, sizeof(d));
    okf(rc > 0, "master: the second mod applied", d);
    okf(strstr(d, "master=") == NULL, "master: not reported a second time - it was already there", d);
    okf(g_write_calls == 1, "master: only the mod's own write happened", d);

    /* TURNING A MOD OFF DOES NOT TOUCH THE MASTER. */
    g_write_calls = 0;
    rc = apply(DOC_MASTER, 0, 0, 0, d, sizeof(d));
    okf(rc > 0 && g_write_calls == 1, "master: reverting a mod leaves the master alone", d);
    ok(g_game[0x1000] == 0xE9, "master: still in place after a revert");

    /* A MASTER THAT CANNOT BE PLACED MUST NOT TAKE A MOD OUTSIDE ITS CAVES DOWN WITH IT. */
    reset();
    put(0x4200, "7405");
    static const char *DOC_FARMASTER =
    "{\"master\":{\"memory\":[{\"offset\":\"7FFFFFFFFFFF\",\"on\":\"90\"}]},"
    "\"mods\":[{\"name\":\"plain\",\"memory\":[{\"offset\":\"4200\",\"on\":\"9090\",\"off\":\"7405\"}]}]}";
    rc = apply(DOC_FARMASTER, 0, 1, 0, d, sizeof(d));
    okf(rc > 0, "master refused: the plain mod still applied", d);
    okf(strstr(d, "master=") == NULL, "master refused: not counted as installed", d);

    /* REMOVING A MASTER needs documented original bytes. The json form has none - and this path is
       called by Disable-all now, where the promise on screen ("removed again when you turn them all
       off") previously had no implementation behind it at all. */
    reset();
    int n = cheat_master_off(DOC_MASTER, 1, FAKE_BASE, 0, why, sizeof(why));
    okf(n < 0 && strstr(why, "does not say what was there") != NULL,
        "master: a json master cannot be removed, and says why", why);

    /* The Trainer form carries ValueOff, so that one can - and it has to FIND what it wrote. */
    static const char *DOC_MASTER_OFF =
    "{\"master\":{\"memory\":[{\"offset\":\"4000\",\"on\":\"9090\",\"off\":\"7405\"}]},\"mods\":[]}";
    reset();
    put(0x4000, "9090");
    n = cheat_master_off(DOC_MASTER_OFF, 1, FAKE_BASE, 0, why, sizeof(why));
    okf(n == 1, "master: one with off bytes is removed", why);
    ok(memcmp(g_game + 0x4000, "\x74\x05", 2) == 0, "master: the original bytes are back");

    /* ...and refuses when the game does not hold what it wrote. */
    reset();
    put(0x4000, "DEAD");
    n = cheat_master_off(DOC_MASTER_OFF, 1, FAKE_BASE, 0, why, sizeof(why));
    okf(n < 0 && strstr(why, "does not hold") != NULL,
        "master: removal refuses when the master is not there", why);
    ok(memcmp(g_game + 0x4000, "\xDE\xAD", 2) == 0, "master: and nothing was put back over it");
}

/* ============================================================================================
 * 7. THE REAL FILE. assets/cheats/json/CUSA01589_01.02.json, as shipped.
 * ========================================================================================== */
static void test_dark_souls_2(void) {
    printf("Dark Souls II's own cheat file, as shipped - and why it cannot be applied\n");
    long flen = 0;
    char path[1024];
    snprintf(path, sizeof(path), "%s/assets/cheats/json/CUSA01589_01.02.json", g_root);
    char *doc = slurp(path, &flen);
    if (!doc) {
        printf("  (the file is not here - skipped)\n");
        return;
    }

    /* WHAT WAS MEASURED ON THE OWNER'S CONSOLE, on a build this file DOES fit (pid 472, base
     * 0x400000, read through the in-game agent):
     *
     *   +0x2077800  40 bytes  all zeros            the master's cave, empty
     *   +0x5BCBA5    6 bytes  8B 83 70 01 00 00    the hook site - and those bytes are inside the cave
     *   +0x2077828  64 bytes  all zeros            the second cave
     *   +0x5C3F70    6 bytes  8B 80 EC 00 00 00    the second hook site, likewise
     *   +0x2077868  36 bytes  all zeros            1 hit kill's own cave
     *   +0x19054E    7 bytes  41 89 87 70 01 00 00 its hook site, exactly its documented off bytes
     *
     * Everything matches. And it is STILL refused, because the routine the master installs does
     * `mov [rip-0x2473807], rbx` from +0x2077807, which resolves to image offset -0x3FC000 - absolute
     * 0x4000, which is not inside any module the process has loaded. The agent answers ST_UNMAPPED
     * for it while 0x400000 reads fine. The game faults the first time that code runs, and that is
     * what the owner saw twice: "1 hit kill" crashing on hitting an enemy, and then the master itself
     * crashing the moment any cheat was pressed. */
    reset();
    memset(g_game + 0x2077800, 0, 0xA0);
    put(0x5BCBA5, "8B8370010000");
    put(0x5C3F70, "8B80EC000000");
    put(0x19054E, "41898770010000");
    put(0x107D1B1, "4429F0");                    /* infinit Consomable, a plain patch elsewhere */

    int idx_1hk = -1, idx_god = -1, idx_cons = -1;
    const char *from = mods_array_start(doc);
    for (int m = 0; from && m < 64; m++) {
        const char *end = NULL;
        const char *blk = next_mod_block(from, &end);
        if (!blk) break;
        from = end;
        char nm[120] = {0};
        json_str_after(blk, "name", nm, sizeof(nm));
        if (strstr(nm, "1 hit kill")) idx_1hk = m;
        if (strstr(nm, "God mode")) idx_god = m;
        if (strstr(nm, "Consomable")) idx_cons = m;
    }
    ok(idx_1hk >= 0 && idx_god >= 0 && idx_cons >= 0,
       "the file still has 1 hit kill, God mode and infinit Consomable");
    if (idx_1hk < 0 || idx_god < 0 || idx_cons < 0) { free(doc); return; }

    char d[256], msg[420];

    /* 1 HIT KILL: its own cave reads the same unmapped slot, so it is refused on its own account. */
    int before = g_write_calls;
    int rc = apply(doc, idx_1hk, 1, 0, d, sizeof(d));
    okf(rc < 0, "1 hit kill: refused", d);
    okf(strstr(d, "noreach=1") != NULL, "1 hit kill: because its code reaches nowhere", d);
    okf(g_write_calls == before, "1 hit kill: and the game was not touched", d);
    ok(memcmp(g_game + 0x19054E, "\x41\x89\x87\x70\x01\x00\x00", 7) == 0,
       "1 hit kill: its hook site still holds the game's own instruction");
    cheat_rc_message(rc, d, 1, msg, sizeof(msg));
    okf(strstr(msg, "does not have") != NULL, "1 hit kill: and the owner is told why", msg);

    /* GOD MODE: a patch inside the master's routine, so it is refused for the master's reason. */
    before = g_write_calls;
    rc = apply(doc, idx_god, 1, 0, d, sizeof(d));
    okf(rc == -6, "God mode: refused as a master problem", d);
    okf(strstr(d, "master_refused=5") != NULL,
        "God mode: with the reason - the master needs memory the game does not have", d);
    okf(g_write_calls == before, "God mode: and the game was not touched", d);
    ok(memcmp(g_game + 0x5BCBA5, "\x8B\x83\x70\x01\x00\x00", 6) == 0,
       "God mode: THE HOOK SITE IS UNTOUCHED - this is the crash that must never come back");
    cheat_rc_message(rc, d, 1, msg, sizeof(msg));
    okf(strstr(msg, "scratch space") != NULL, "God mode: and the owner is told what it is", msg);

    /* AND THE ONE THAT DOES WORK. A plain byte patch elsewhere in the same file owes the master
       nothing, and refusing it would be its own bug. */
    rc = apply(doc, idx_cons, 1, 0, d, sizeof(d));
    okf(rc > 0, "infinit Consomable: still applies", d);
    ok(memcmp(g_game + 0x107D1B1, "\x83\xE8\x00", 3) == 0, "infinit Consomable: its bytes are in");
    rc = apply(doc, idx_cons, 0, 0, d, sizeof(d));
    okf(rc > 0, "infinit Consomable: and reverts", d);
    ok(memcmp(g_game + 0x107D1B1, "\x44\x29\xF0", 3) == 0,
       "infinit Consomable: the game's own bytes are back");
    free(doc);
}

/* ============================================================================================
 * 8. Trainer XML: <StartUP> becomes a master, <Section> travels with the entry
 * ========================================================================================== */
static void test_shn_conversion(void) {
    printf("Trainer XML keeps its master code and its section\n");
    static const char *XML =
    "<?xml version=\"1.0\" encoding=\"utf-8\"?>\n"
    "<Trainer Game=\"G\" Cusa=\"CUSA00103\" Version=\"01.04\" Process=\"eboot.bin\">\n"
    "  <Items />\n"
    "  <StartUP Text=\"Master Code 1 (Must Be On)\" description=\"\">\n"
    "    <Cheatline><Offset>1353874</Offset><Section>0</Section>"
    "<ValueOn>48-81-FB-80</ValueOn><ValueOff>00-00-00-00</ValueOff></Cheatline>\n"
    "    <Cheatline><Offset>3F3960</Offset><Section>0</Section>"
    "<ValueOn>E9-0F-FF-F5-00-90-90</ValueOn><ValueOff>C4-C1-7A-11-4C-CA-0C</ValueOff></Cheatline>\n"
    "  </StartUP>\n"
    "  <Cheat Text=\"Godmode\">\n"
    "    <Cheatline><Offset>668DA3</Offset><Section>11</Section>"
    "<ValueOn>90-90-90-90</ValueOn><ValueOff>C5-FA-11-00</ValueOff></Cheatline>\n"
    "  </Cheat>\n"
    "</Trainer>\n";
    char *j = shn_xml_to_json(XML, strlen(XML));
    ok(j != NULL, "the converter produced a document");
    if (!j) return;

    okf(strstr(j, "\"master\"") != NULL, "the StartUP block became a master", j);
    okf(strstr(j, "\"dropped\"") == NULL, "and nothing was dropped from it", j);
    okf(strstr(j, "\"1353874\"") != NULL, "the master's first offset came across", j);
    okf(strstr(j, "\"off\":\"C4C17A114CCA0C\"") != NULL,
        "the master kept its original bytes, so it can be removed again", j);
    okf(strstr(j, "\"section\":\"11\"") != NULL, "the cheat's section came across", j);
    okf(strstr(j, "\"name\":\"Godmode\"") != NULL, "the cheat itself is still there", j);
    okf(strstr(j, "Master Code 1") == NULL, "the master is NOT also listed as a cheat", j);
    /* one "mods" array, not two */
    const char *m1 = strstr(j, "\"mods\"");
    okf(m1 && !strstr(m1 + 6, "\"mods\""), "exactly one mods array", j);

    /* and it parses: the master is findable and readable through the same code the engine uses */
    const char *mend = NULL;
    const char *mblk = cheat_master_span(j, &mend);
    ok(mblk != NULL && mend != NULL, "cheat_master_span finds it");
    if (mblk) {
        cheat_entry_t es[8];
        int n = parse_mod_entries_ex(mblk, mend, es, 8, NULL);
        okf(n == 2, "both master entries parse", j);
        if (n == 2) ok(es[0].off_len == 4 && es[1].off_len == 7, "with their original bytes");
    }
    free(j);
}


/* ============================================================================================
 * 9. EVERY Trainer file in the library, converted. A sweep, because the converter was just changed
 *    and 1,761 real files are a better test of it than any fixture.
 * ========================================================================================== */
static void test_shn_library(void) {
    printf("every .shn in the library converts\n");
    char dir[1024];
    snprintf(dir, sizeof(dir), "%s/assets/cheats/shn", g_root);
    DIR *d = opendir(dir);
    if (!d) { printf("  (the library is not here - skipped)\n"); return; }
    int files = 0, bad = 0, with_master = 0, removable = 0, mods_total = 0, no_mods = 0;
    int refused16 = 0;
    struct dirent *e;
    while ((e = readdir(d)) != NULL) {
        const char *n = e->d_name;
        size_t ln = strlen(n);
        if (ln < 5 || strcmp(n + ln - 4, ".shn") != 0) continue;
        char path[1200];
        snprintf(path, sizeof(path), "%s/%s", dir, n);
        long fl = 0;
        char *xml = slurp(path, &fl);
        if (!xml) continue;
        files++;
        int utf16 = (fl >= 2) &&
                    (((unsigned char)xml[0] == 0xFF && (unsigned char)xml[1] == 0xFE) ||
                     ((unsigned char)xml[0] == 0xFE && (unsigned char)xml[1] == 0xFF) ||
                     xml[0] == 0 || xml[1] == 0);
        char *j = shn_xml_to_json(xml, (size_t)fl);
        free(xml);
        /* A UTF-16 FILE IS SUPPOSED TO COME BACK NULL. Four in the shipped library are, and the
           converter used to return a well-formed document with no cheats in it - which reads as
           "this game has nothing" instead of "this file cannot be read". */
        if (utf16) { refused16 += (j == NULL); if (j) bad++; free(j); continue; }
        if (!j) { bad++; continue; }
        /* exactly one mods array, and it opens and closes */
        const char *m = strstr(j, "\"mods\":[");
        if (!m || strstr(m + 7, "\"mods\":[")) { bad++; free(j); continue; }
        /* the document must end as an object */
        size_t jl = strlen(j);
        if (jl < 4 || j[jl - 1] != '}') { bad++; free(j); continue; }
        int rem = 0;
        if (cheat_master_info(j, &rem)) { with_master++; if (rem) removable++; }
        /* count the mods the engine would see */
        int cnt = 0;
        const char *from = mods_array_start(j);
        for (int i = 0; from && i < CHEAT_MAX_MODS; i++) {
            const char *end = NULL;
            const char *blk = next_mod_block(from, &end);
            if (!blk) break;
            from = end;
            cnt++;
        }
        mods_total += cnt;
        if (!cnt) no_mods++;
        free(j);
    }
    closedir(d);
    printf("  %d file(s), %d mod(s), %d with a master code (%d of those removable)\n",
           files, mods_total, with_master, removable);
    okf(files > 1000, "the sweep actually read the library", "file count");
    okf(refused16 == 4, "the four UTF-16 files are refused, not silently emptied", "utf16 count");
    okf(bad == 0, "every file produced one well-formed document", "malformed conversions");
    /* A CONVERTER THAT EMITS NOTHING WOULD PASS THE TWO ABOVE. These are the measured numbers from
       the library as shipped: 1,761 Trainer files, 73 of them carrying a StartUP block, and every
       StartUP carries ValueOff so all 73 are removable. A change that moves these is either a
       library change or a bug, and either way somebody should look. */
    okf(with_master == 73, "73 files carry a master code, as measured", "master count");
    okf(removable == with_master, "every Trainer master documents its original bytes", "removable");
    okf(no_mods < 20, "almost every file yields at least one mod", "empty conversions");
}

/* ============================================================================================
 * 10. THE CRASH I CAUSED, and the four gates that make it impossible.
 *
 * 3.82.0 installed a master code with no gate at all: read the bytes, and if they were not already
 * what it was about to write, write anyway. What actually killed the owner's game was the cave it
 * then ran, which addresses absolute 0x4000 - see test_unreachable_scratch, and the note there about
 * reading an image offset as an absolute address and drawing the wrong conclusion from it.
 *
 * The cases below are a mixture, and each says which it is: the ones built from memory measured on
 * the owner's console, and the SYNTHETIC wrong-build ones, which test what the old code permitted
 * rather than what it did.
 * ========================================================================================== */

/* Dark Souls II's master, exactly as shipped, and the game as it looks on a build it fits:
 *   cave 1  +0x2077800  40 bytes, currently zeros
 *   hook 1  +0x5BCBA5    6 bytes, currently 8B8370010000 - which appears inside cave 1
 *   cave 2  +0x2077828  64 bytes, currently zeros
 *   hook 2  +0x5C3F70    6 bytes, currently 8B80EC000000 - which appears inside cave 2
 * with one difference: the scratch. The real file's cave does mov [rip-0x2473807], rbx, which lands
 * at image offset -0x3FC000, and that is tested separately below because it is refused.
 */
static const char *DOC_DS2LIKE =
"{\"name\":\"t\",\"master\":{\"challenged\":\"yes\",\"memory\":["
 "{\"offset\":\"5000\",\"on\":\"90909090908B8370010000909090E9F6AFFFFF\"},"
 "{\"offset\":\"1000\",\"on\":\"E9FB3F000090\"}]},"
"\"mods\":["
 "{\"name\":\"inside the cave\",\"memory\":[{\"offset\":\"5005\",\"on\":\"8B8378010000\",\"off\":\"8B8370010000\"}]},"
 "{\"name\":\"somewhere else\",\"memory\":[{\"offset\":\"7000\",\"on\":\"9090\",\"off\":\"7405\"}]}]}";

static void test_master_gates(void) {
    printf("a master code is gated like everything else\n");
    char d[256], msg[420];

    /* ---- the build it fits: cave empty, hook holding code the cave re-executes ---------------- */
    reset();
    memset(g_game + 0x5000, 0, 19);            /* cave 1, empty */
    put(0x1000, "8B8370010000");               /* hook 1, the instruction the cave steals */
    int rc = apply(DOC_DS2LIKE, 0, 1, 0, d, sizeof(d));
    okf(rc > 0, "right build: the cave-resident cheat applied", d);
    okf(strstr(d, "master=2") != NULL, "right build: both master entries went in", d);
    ok(g_game[0x1000] == 0xE9, "right build: the hook is installed");
    ok(memcmp(g_game + 0x5005, "\x8B\x83\x78\x01\x00\x00", 6) == 0,
       "right build: the cheat patched the master's routine");

    /* ---- A BUILD IT DOES NOT FIT. A SYNTHETIC case, and labelled as one: the hook site holds
     *      `pop rbp; ret` and the cave holds unwind-table bytes instead of zeros. The owner's console
     *      was NOT in this state - it reports APP_VER 01.02 for a 01.02 file and both hook sites hold
     *      exactly the bytes their cave re-executes. This is what the old code PERMITTED, which is
     *      worth a test of its own; what actually crashed the game is test_unreachable_scratch. --- */
    reset();
    put(0x5000, "B42500009C8EB6FE0B0000000000000010000000");   /* unwind-table shaped bytes */
    put(0x1000, "5DC3488B03488B4030");                          /* pop rbp; ret; ... */
    int before = g_write_calls;
    rc = apply(DOC_DS2LIKE, 0, 1, 0, d, sizeof(d));
    okf(rc == -6, "wrong build: refused, and refused as a master problem", d);
    okf(strstr(d, "master_refused=") != NULL, "wrong build: the detail names the master", d);
    okf(g_write_calls == before, "WRONG BUILD: NOTHING WAS WRITTEN - this is the crash", d);
    ok(memcmp(g_game + 0x1000, "\x5D\xC3", 2) == 0,
       "wrong build: `pop rbp; ret` is still there, untouched");
    cheat_rc_message(rc, d, 1, msg, sizeof(msg));
    okf(strstr(msg, "master code") != NULL, "wrong build: the owner is told it is the master", msg);

    /* the cave is empty but the HOOK holds something the cave does not re-execute */
    reset();
    memset(g_game + 0x5000, 0, 19);
    put(0x1000, "5DC3488B03488B4030");
    before = g_write_calls;
    rc = apply(DOC_DS2LIKE, 0, 1, 0, d, sizeof(d));
    okf(rc == -6 && g_write_calls == before, "a hook site that does not hold the stolen code is refused", d);

    /* the hook is right but the CAVE is not empty */
    reset();
    put(0x5000, "B42500009C8EB6FE0B0000000000000010000000");
    put(0x1000, "8B8370010000");
    before = g_write_calls;
    rc = apply(DOC_DS2LIKE, 0, 1, 0, d, sizeof(d));
    okf(rc == -6 && g_write_calls == before, "a cave that is not empty is refused", d);

    /* ---- A MOD ELSEWHERE IN THE SAME FILE MUST STILL WORK. Refusing it would be its own bug:
     *      Dark Souls II's "infinit Consomable" is a plain byte patch and does not need the master. */
    reset();
    put(0x5000, "B42500009C8EB6FE0B0000000000000010000000");   /* master cannot be installed */
    put(0x1000, "5DC3488B03488B4030");
    put(0x7000, "7405");                                        /* ...but this mod is ready */
    rc = apply(DOC_DS2LIKE, 1, 1, 0, d, sizeof(d));
    okf(rc > 0, "a mod outside the master's caves still applies", d);
    ok(memcmp(g_game + 0x7000, "\x90\x90", 2) == 0, "and its bytes are in the game");
    okf(strstr(d, "master=") == NULL, "without pretending the master went in", d);

    /* ---- THE MASTER MUST NOT GO IN WHEN THE MOD IS GOING TO BE REFUSED --------------------------
     * This was real: the master was written at the top of cheat_apply_blk and the mod was gated
     * afterwards, so a refused mod left an irreversible master behind in a running game. */
    reset();
    memset(g_game + 0x5000, 0, 19);
    put(0x1000, "8B8370010000");
    put(0x7000, "DEADBEEF");                    /* mod 1's bytes match neither state */
    before = g_write_calls;
    rc = apply(DOC_DS2LIKE, 1, 1, 0, d, sizeof(d));
    okf(rc < 0, "a mod that cannot be written is refused", d);
    okf(g_write_calls == before,
        "AND THE MASTER DID NOT GO IN EITHER - nothing was written at all", d);
    ok(g_game[0x1000] == 0x8B, "the hook site is untouched");
}

/* ============================================================================================
 * 11. THE SCRATCH. The reason "1 hit kill" crashed on hitting an enemy, before any of this.
 *     Its cave does `cmp r15, [rip-0x2473876]`, which resolves to image offset -0x3FC000. There is
 *     no such place in an executable, and absolute 0x4000 is not inside any module this process has
 *     loaded - the agent answers ST_UNMAPPED for it while 0x400000 reads fine.
 * ========================================================================================== */
static void test_unreachable_scratch(void) {
    printf("a cheat whose own code reaches nowhere is refused\n");
    char d[256], msg[420];

    /* 48 89 1D <disp32> chosen so the target lands BELOW the image - the shipped shape. */
    static const char *DOC_NEG =
    "{\"mods\":[{\"name\":\"needs scratch below the image\",\"memory\":["
    "{\"offset\":\"5000\",\"on\":\"48891D0000C0FF9090909090\",\"off\":\"000000000000000000000000\"}]}]}";
    reset();
    memset(g_game + 0x5000, 0, 12);
    int before = g_write_calls;
    int rc = apply(DOC_NEG, 0, 1, 0, d, sizeof(d));
    okf(rc == -4, "a negative RIP target is refused", d);
    okf(strstr(d, "noreach=1") != NULL, "and counted as unreachable, not as a version mismatch", d);
    okf(g_write_calls == before, "and nothing was written", d);
    cheat_rc_message(rc, d, 1, msg, sizeof(msg));
    okf(strstr(msg, "does not have") != NULL, "and the owner is told what it is", msg);

    /* the same shape, but the target is inside the game: allowed. */
    static const char *DOC_OK =
    "{\"mods\":[{\"name\":\"scratch in the image\",\"memory\":["
    "{\"offset\":\"5000\",\"on\":\"48891D001000009090909090\",\"off\":\"000000000000000000000000\"}]}]}";
    reset();
    memset(g_game + 0x5000, 0, 12);
    rc = apply(DOC_OK, 0, 1, 0, d, sizeof(d));
    okf(rc > 0, "a RIP target inside the game is allowed", d);

    /* and a target past the END of what can be read is refused too */
    static const char *DOC_FAR =
    "{\"mods\":[{\"name\":\"scratch past the image\",\"memory\":["
    "{\"offset\":\"5000\",\"on\":\"48891D000030029090909090\",\"off\":\"000000000000000000000000\"}]}]}";
    reset();
    memset(g_game + 0x5000, 0, 12);
    before = g_write_calls;
    rc = apply(DOC_FAR, 0, 1, 0, d, sizeof(d));
    okf(rc == -4 && g_write_calls == before, "a RIP target past the game's memory is refused", d);

    /* THE DECODER ITSELF, against the real bytes from the shipped file. */
    unsigned char cave[64];
    int n = hex2bytes("48891DF9C7B8FD8B8370010000898370010000C5FA1083B4010000C5FA1183B4010000E9835354FE",
                      cave, sizeof(cave));
    ok(n == 40, "the real Dark Souls II master cave parses to 40 bytes");
    long long t[RIP_MAX_OPS];
    int got = rip_targets(cave, n, 0x2077800, t, RIP_MAX_OPS);
    okf(got >= 1, "the decoder finds its RIP operand", "count");
    int found = 0;
    for (int i = 0; i < got; i++) if (t[i] == -0x3FC000) found = 1;
    ok(found, "and resolves it to image offset -0x3FC000, exactly as measured on the console");
}

/* ============================================================================================
 * 12. ASKED, NOT TOLD. check_only runs the whole decision and writes nothing - and it must agree
 *     with the real thing, or it is worse than useless.
 * ========================================================================================== */
static int apply_ck(const char *doc, int index, int want_on, char *detail, size_t dsz) {
    const char *from = mods_array_start(doc);
    for (int m = 0; from; m++) {
        const char *end = NULL;
        const char *blk = next_mod_block(from, &end);
        if (!blk) break;
        from = end;
        if (m == index)
            return cheat_apply_blk(doc, 0, blk, end, index, want_on, 1, FAKE_BASE, 0, 1,
                                   detail, dsz);
    }
    return -99;
}

static void test_check_only(void) {
    printf("the engine can be asked what it would do\n");
    char dck[256], dre[256];
    static const char *DOC =
    "{\"mods\":[{\"name\":\"two places\",\"memory\":["
    "{\"offset\":\"1200\",\"on\":\"9090\",\"off\":\"7405\"},"
    "{\"offset\":\"1300\",\"on\":\"9090\",\"off\":\"7405\"}]}]}";

    /* a mod that WOULD apply */
    reset();
    put(0x1200, "7405");
    put(0x1300, "7405");
    int before = g_write_calls;
    int rc = apply_ck(DOC, 0, 1, dck, sizeof(dck));
    okf(rc == 2, "check: it says it would write both entries", dck);
    okf(strstr(dck, "check=1") != NULL, "check: and marks itself as a check", dck);
    okf(g_write_calls == before, "check: WITHOUT WRITING ANYTHING", dck);
    ok(memcmp(g_game + 0x1200, "\x74\x05", 2) == 0, "check: the game is exactly as it was");
    /* ...and the real thing agrees */
    rc = apply(DOC, 0, 1, 0, dre, sizeof(dre));
    okf(rc == 2, "check: the real apply writes the same number", dre);

    /* a mod that would be REFUSED: the check must give the same reason, not a different one */
    reset();
    put(0x1200, "7405");
    put(0x1300, "DEAD");
    before = g_write_calls;
    rc = apply_ck(DOC, 0, 1, dck, sizeof(dck));
    okf(rc == -4, "check: a refusal is reported as a refusal", dck);
    okf(strstr(dck, "mismatch=1") != NULL, "check: with the real reason", dck);
    okf(g_write_calls == before, "check: and still nothing written", dck);
    rc = apply(DOC, 0, 1, 0, dre, sizeof(dre));
    okf(rc == -4 && strstr(dre, "mismatch=1") != NULL,
        "check: and the real apply refuses for the same reason", dre);

    /* THE ONE THAT MATTERS: the real Dark Souls II master, asked rather than tried. */
    long flen = 0;
    char path[1024];
    snprintf(path, sizeof(path), "%s/assets/cheats/json/CUSA01589_01.02.json", g_root);
    char *doc = slurp(path, &flen);
    if (doc) {
        reset();
        memset(g_game + 0x2077800, 0, 0xA0);
        put(0x5BCBA5, "8B8370010000");
        put(0x5C3F70, "8B80EC000000");
        put(0x19054E, "41898770010000");
        int idx = -1;
        const char *from = mods_array_start(doc);
        for (int m = 0; from && m < 64; m++) {
            const char *end = NULL;
            const char *blk = next_mod_block(from, &end);
            if (!blk) break;
            from = end;
            char nm[120] = {0};
            json_str_after(blk, "name", nm, sizeof(nm));
            if (strstr(nm, "God mode")) { idx = m; break; }
        }
        if (idx >= 0) {
            before = g_write_calls;
            rc = apply_ck(doc, idx, 1, dck, sizeof(dck));
            okf(rc == -6 && strstr(dck, "master_refused=5") != NULL,
                "check: Dark Souls II God mode is refused, and says exactly why", dck);
            okf(g_write_calls == before, "check: and the game was never touched", dck);
        }
        free(doc);
    }
}

/* ============================================================================================
 * 13. THE SIGNATURE SEARCH. Parsing first, because a pattern that parses wrong searches for the
 *     wrong thing and finds it - which is worse than finding nothing.
 * ========================================================================================== */
static void test_sig_parse(void) {
    printf("a signature is parsed, or refused\n");
    pms_sig_t g;

    ok(sig_parse("488B05", &g) == 3 || g.n == 3, "plain hex parses");
    ok(sig_parse("48 8B 05 89", &g) == 4 && g.n == 4, "spaces are separators");
    ok(sig_parse("48-8B-05-89", &g) == 4, "and so are dashes - the Trainer format uses them");
    ok(sig_parse("48,8B,05,89", &g) == 4, "and commas");

    /* the wildcard, in all three spellings the wild uses */
    ok(sig_parse("488B05????????89", &g) == 8, "?? is a wildcard");
    if (g.n == 8) {
        ok(g.m[0] == 1 && g.m[1] == 1 && g.m[2] == 1, "the real bytes are marked real");
        ok(g.m[3] == 0 && g.m[4] == 0 && g.m[5] == 0 && g.m[6] == 0, "the wildcards are marked free");
        ok(g.b[0] == 0x48 && g.b[2] == 0x05 && g.b[7] == 0x89, "and the bytes are the bytes");
    }
    ok(sig_parse("488B05********89", &g) == 8, "** is a wildcard too");
    ok(sig_parse("488B05xxxxxxxx89", &g) == 8, "and xx");

    /* what must be REFUSED, because each of these searches for something useless */
    ok(sig_parse("48", &g) == 0, "one byte is not a signature");
    ok(sig_parse("488B", &g) == 0, "two bytes are not a signature");
    ok(sig_parse("????????", &g) == 0, "all wildcards matches everywhere - refused");
    ok(sig_parse("48??????", &g) == 0, "one real byte in four is not enough");
    ok(sig_parse("48 8B ZZ 89", &g) == 0, "not-hex is refused rather than skipped");
    ok(sig_parse("", &g) == 0, "and so is nothing");
    ok(sig_parse(NULL, &g) == 0, "and NULL");

    /* the longest thing it will take, and one byte past it */
    char big[SIG_MAX * 2 + 8];
    for (int i = 0; i < SIG_MAX; i++) { big[i * 2] = '9'; big[i * 2 + 1] = '0'; }
    big[SIG_MAX * 2] = 0;
    ok(sig_parse(big, &g) == SIG_MAX, "a full-length signature is accepted");
    snprintf(big + SIG_MAX * 2, 8, "90");
    ok(sig_parse(big, &g) == SIG_MAX, "and a longer one is truncated, not overrun");
}

static void test_sig_scan(void) {
    printf("the search finds what is there, once, including across a chunk boundary\n");
    /* sig_start runs a thread against mem_read, which here reads the fake game - so this is the real
       search code over real memory, with the answers known because we put them there. */
    reset();
    put(0x1000, "48891DF9C7B8FD");
    put(0x8000, "48891DAABBCCDD");
    /* and one straddling a 16 KB chunk boundary: SIG_CHUNK is 16384, so place it at 16382 */
    put(SIG_CHUNK - 2, "48891D11223344");

    int rc = sig_start("48891D????????", 1, FAKE_BASE, 0, 0x9000);
    okf(rc == 0, "the search starts", "rc");
    /* wait for it, with a ceiling so a hang is a failure rather than a hang */
    char st[2048];
    for (int i = 0; i < 400; i++) {
        sig_status_json(st, sizeof(st));
        if (strstr(st, "\"active\":false")) break;
        usleep(20000);
    }
    okf(strstr(st, "\"active\":false") != NULL, "and finishes", st);
    okf(strstr(st, "\"found\":3") != NULL, "finding all three", st);
    okf(strstr(st, "\"1000\"") != NULL, "the first one, by image offset", st);
    okf(strstr(st, "\"8000\"") != NULL, "the second one", st);
    okf(strstr(st, "\"3FFE\"") != NULL,
        "AND THE ONE ACROSS THE CHUNK BOUNDARY - exactly once", st);
    okf(strstr(st, "\"truncated\":false") != NULL, "nothing was dropped", st);

    /* a signature that is not there finds nothing, and says so rather than guessing */
    rc = sig_start("EFBEADDE1122", 1, FAKE_BASE, 0, 0x9000);
    okf(rc == 0, "a second search starts once the first is done", "rc");
    for (int i = 0; i < 400; i++) {
        sig_status_json(st, sizeof(st));
        if (strstr(st, "\"active\":false")) break;
        usleep(20000);
    }
    okf(strstr(st, "\"found\":0") != NULL, "a signature that is absent finds nothing", st);

    /* ONE AT A TIME. Two sweeps over the same channel would interleave their reads. */
    rc = sig_start("48891D????????", 1, FAKE_BASE, 0, 0x2000000);
    okf(rc == 0, "a long search starts", "rc");
    int rc2 = sig_start("48891D????????", 1, FAKE_BASE, 0, 0x1000);
    okf(rc2 == -1, "a second one while it runs is refused", "rc2");
    sig_cancel();
    for (int i = 0; i < 400; i++) {
        sig_status_json(st, sizeof(st));
        if (strstr(st, "\"active\":false")) break;
        usleep(20000);
    }
    okf(strstr(st, "\"active\":false") != NULL, "and cancel stops it", st);

    /* a bad pattern and a bad range are refused before a thread is made */
    okf(sig_start("48", 1, FAKE_BASE, 0, 0x1000) == -2, "a short pattern is refused", "rc");
    okf(sig_start("48891D11", 1, FAKE_BASE, 0x1000, 0x1000) == -3, "an empty range is refused", "rc");
    okf(sig_start("48891D11", 1, FAKE_BASE, 0, SIG_SPAN_MAX + 1) == -3,
        "a span wider than one sweep is refused", "rc");
    okf(sig_start("48891D11", 0, FAKE_BASE, 0, 0x1000) == -5,
        "no game means no search - not a confident zero", "rc");
    okf(sig_start("48891D11", 1, 0, 0, 0x1000) == -5, "and neither does no base", "rc");
}

/* ============================================================================================
 * 14. TWO CHEATS IN ONE MASTER CAVE. The audit found that only the first could be on: the master's
 *     idempotence test was an exact memcmp of the whole cave, and a cave is BY DESIGN mutated by the
 *     cheats living in it. The second was then refused as "a different build".
 *     Measured in the shipped library: 43 mod entries start inside a master span, 9 files have two or
 *     more sharing one, and CUSA05574_01.50 has eight in a single cave.
 * ========================================================================================== */
static const char *DOC_TWO_IN_CAVE =
"{\"master\":{\"challenged\":\"yes\",\"memory\":["
 "{\"offset\":\"5000\",\"on\":\"90909090909090909090909090909090\"}]},"
"\"mods\":["
 "{\"name\":\"first in the cave\",\"memory\":[{\"offset\":\"5002\",\"on\":\"B801000000\",\"off\":\"9090909090\"}]},"
 "{\"name\":\"second in the cave\",\"memory\":[{\"offset\":\"5008\",\"on\":\"B802000000\",\"off\":\"9090909090\"}]}]}";

static void test_two_in_one_cave(void) {
    printf("two cheats can share one master cave\n");
    char d[256], msg[420];

    reset();
    memset(g_game + 0x5000, 0, 16);          /* the cave, empty */
    int rc = apply(DOC_TWO_IN_CAVE, 0, 1, 0, d, sizeof(d));
    okf(rc > 0, "the first cheat applies", d);
    okf(strstr(d, "master=1") != NULL, "and the master goes in with it", d);
    ok(memcmp(g_game + 0x5002, "\xB8\x01\x00\x00\x00", 5) == 0, "its bytes are in the cave");

    /* THE ONE THE AUDIT FOUND. The cave now differs from the master's own bytes at 5002-5006, so an
       exact memcmp says "not in place", it is not a hook, and it is not all-zero either. */
    int before = g_write_calls;
    rc = apply(DOC_TWO_IN_CAVE, 1, 1, 0, d, sizeof(d));
    okf(rc > 0, "THE SECOND CHEAT ALSO APPLIES - this is the defect", d);
    okf(strstr(d, "master=") == NULL, "and the master is not written again", d);
    okf(g_write_calls == before + 1, "exactly one write: the cheat's own", d);
    ok(memcmp(g_game + 0x5008, "\xB8\x02\x00\x00\x00", 5) == 0, "the second cheat is in");
    ok(memcmp(g_game + 0x5002, "\xB8\x01\x00\x00\x00", 5) == 0,
       "AND THE FIRST ONE IS STILL THERE - the master did not overwrite it");
    ok(g_game[0x5000] == 0x90 && g_game[0x5001] == 0x90, "the master's own bytes are intact");

    /* both off again, in either order, and the cave comes back to the master's routine */
    rc = apply(DOC_TWO_IN_CAVE, 0, 0, 0, d, sizeof(d));
    okf(rc > 0, "the first reverts", d);
    rc = apply(DOC_TWO_IN_CAVE, 1, 0, 0, d, sizeof(d));
    okf(rc > 0, "the second reverts", d);
    int clean = 1;
    for (int i = 0; i < 16; i++) if (g_game[0x5000 + i] != 0x90) clean = 0;
    ok(clean, "and the cave is the master's routine again, byte for byte");

    /* A WRONG BUILD STILL FAILS. The relaxation must not become "anything goes": fill the cave with
       bytes no mod in the document declares and it has to be refused. */
    reset();
    put(0x5000, "DEADBEEFDEADBEEFDEADBEEFDEADBEEF");
    before = g_write_calls;
    rc = apply(DOC_TWO_IN_CAVE, 0, 1, 0, d, sizeof(d));
    okf(rc == -6, "a cave full of something else is still refused", d);
    okf(g_write_calls == before, "and nothing is written", d);
    cheat_rc_message(rc, d, 1, msg, sizeof(msg));
    okf(msg[0] != 0, "with a sentence", msg);

    /* ...and a cave that differs ONLY where a mod declares a run, but with bytes no mod documents,
       is still accepted - because the position is what the file licenses, and the engine cannot know
       which of a hundred cheats put them there. Asserted so the limit is written down. */
    reset();
    memset(g_game + 0x5000, 0, 16);
    rc = apply(DOC_TWO_IN_CAVE, 0, 1, 0, d, sizeof(d));      /* installs the master first */
    okf(rc > 0, "(setup) the master is in", d);
    put(0x5002, "1122334455");                /* inside mod 0's declared run, undocumented bytes */
    rc = apply(DOC_TWO_IN_CAVE, 1, 1, 0, d, sizeof(d));
    okf(rc > 0, "a declared position with undocumented bytes is tolerated (a documented limit)", d);
}


/* ============================================================================================
 * 15. TWO <StartUP> BLOCKS IN ONE FILE. 126 blocks across 73 shipped files, so 36 of them carry more
 *     than one - and the converter read only the first, which installs half a routine.
 * ========================================================================================== */
static void test_two_startups(void) {
    printf("every StartUP block becomes part of the one master\n");
    static const char *XML =
    "<Trainer Game=\"G\" Cusa=\"CUSA00001\" Version=\"01.00\" Process=\"eboot.bin\">\n"
    "  <StartUP Text=\"Master Code 1\">\n"
    "    <Cheatline><Offset>1000</Offset><ValueOn>90-90-90-90</ValueOn>"
    "<ValueOff>00-00-00-00</ValueOff></Cheatline>\n"
    "  </StartUP>\n"
    "  <StartUP Text=\"Master Code 2\">\n"
    "    <Cheatline><Offset>2000</Offset><ValueOn>74-05-90-90</ValueOn>"
    "<ValueOff>11-22-33-44</ValueOff></Cheatline>\n"
    "  </StartUP>\n"
    "  <Cheat Text=\"Godmode\">\n"
    "    <Cheatline><Offset>3000</Offset><ValueOn>90-90</ValueOn>"
    "<ValueOff>74-05</ValueOff></Cheatline>\n"
    "  </Cheat>\n"
    "</Trainer>\n";
    char *j = shn_xml_to_json(XML, strlen(XML));
    ok(j != NULL, "it converts");
    if (!j) return;
    okf(strstr(j, "\"1000\"") != NULL, "the FIRST master's offset is there", j);
    okf(strstr(j, "\"2000\"") != NULL, "AND THE SECOND'S - this is the defect", j);
    okf(strstr(j, "\"3000\"") != NULL, "and the cheat itself", j);
    /* one master object, not two */
    const char *m1 = strstr(j, "\"master\"");
    okf(m1 && !strstr(m1 + 8, "\"master\""), "exactly one master object", j);
    const char *mm = strstr(j, "\"mods\"");
    okf(mm && !strstr(mm + 6, "\"mods\""), "and exactly one mods array", j);
    /* and both parse through the engine's own reader */
    const char *mend = NULL;
    const char *mblk = cheat_master_span(j, &mend);
    ok(mblk != NULL, "the master span is found");
    if (mblk) {
        cheat_entry_t es[8];
        int n = parse_mod_entries_ex(mblk, mend, es, 8, NULL);
        okf(n == 2, "BOTH master entries parse", j);
    }
    free(j);
}


/* ============================================================================================
 * 16. A BRACE IN A CHEAT'S NAME. next_mod_block counted every brace, including the ones inside a
 *     name - and ten mods in the shipped library have them. Nine are balanced and survived by luck;
 *     CUSA29102_01.01's "Max Items {after using have 2)" is not, and the engine walked 4 of that
 *     file's 5 mods with "Max experience" swallowed into the block before it. A block that has eaten
 *     its neighbour applies BOTH memory arrays: pressing one cheat wrote another.
 * ========================================================================================== */
static void test_brace_in_name(void) {
    printf("a brace in a cheat name is text, not structure\n");
    static const char *DOC =
    "{\"mods\":["
     "{\"name\":\"first\",\"memory\":[{\"offset\":\"1400\",\"on\":\"9090\",\"off\":\"7405\"}]},"
     "{\"name\":\"Max Items {after using have 2)\",\"memory\":[{\"offset\":\"1500\",\"on\":\"9090\",\"off\":\"7405\"}]},"
     "{\"name\":\"swallowed\",\"memory\":[{\"offset\":\"1600\",\"on\":\"9090\",\"off\":\"7405\"}]}]}";

    /* all three blocks are walkable */
    int n = 0;
    const char *from = mods_array_start(DOC);
    for (int m = 0; from && m < 16; m++) {
        const char *end = NULL;
        const char *blk = next_mod_block(from, &end);
        if (!blk) break;
        from = end;
        n++;
    }
    okf(n == 3, "all three mods are walked, not two", "count");

    /* and the unbalanced one applies ONE entry, not two */
    reset();
    put(0x1400, "7405");
    put(0x1500, "7405");
    put(0x1600, "7405");
    char d[256];
    int rc = apply(DOC, 1, 1, 0, d, sizeof(d));
    okf(rc == 1, "the brace-named cheat writes exactly one entry", d);
    ok(memcmp(g_game + 0x1500, "\x90\x90", 2) == 0, "its own bytes are in");
    ok(memcmp(g_game + 0x1600, "\x74\x05", 2) == 0,
       "AND THE NEXT CHEAT WAS NOT APPLIED - this is the defect");

    /* the swallowed one is reachable by index, which is how a cheat is applied */
    rc = apply(DOC, 2, 1, 0, d, sizeof(d));
    okf(rc == 1, "the cheat after it is reachable by index", d);
    ok(memcmp(g_game + 0x1600, "\x90\x90", 2) == 0, "and it is the right one");

    /* an escaped quote inside a name must not end the string early either */
    static const char *DOC_ESC =
    "{\"mods\":["
     "{\"name\":\"he said \\\"{\\\" and left\",\"memory\":[{\"offset\":\"1700\",\"on\":\"9090\",\"off\":\"7405\"}]},"
     "{\"name\":\"after\",\"memory\":[{\"offset\":\"1800\",\"on\":\"9090\",\"off\":\"7405\"}]}]}";
    n = 0;
    from = mods_array_start(DOC_ESC);
    for (int m = 0; from && m < 16; m++) {
        const char *end = NULL;
        const char *blk = next_mod_block(from, &end);
        if (!blk) break;
        from = end;
        n++;
    }
    okf(n == 2, "an escaped quote in a name does not end the string early", "count");
}

int main(int argc, char **argv) {
    if (argc > 1 && argv[1][0]) g_root = argv[1];
    g_game = (unsigned char *)malloc(FAKE_SIZE);
    if (!g_game) { printf("out of memory\n"); return 1; }
    printf("the cheat engine, compiled here, against a fake game\n\n");
    test_section_parse();
    test_refusal_reasons();
    test_whole_or_nothing();
    test_normal_paths();
    test_write_failure();
    test_master();
    test_dark_souls_2();
    test_shn_conversion();
    test_shn_library();
    test_master_gates();
    test_unreachable_scratch();
    test_check_only();
    test_sig_parse();
    test_sig_scan();
    test_two_in_one_cave();
    test_two_startups();
    test_brace_in_name();
    printf("\n%d check(s), %d failure(s)\n", g_ran, g_fail);
    return g_fail ? 1 : 0;
}

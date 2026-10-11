/* PKG MUTANT SHOP - the in-game agent for the PS4 cheat engine. FILE-CHANNEL EDITION.
 *
 * WHY THIS EXISTS AT ALL. Our cheat engine on the PS5 reaches a running game's memory from outside
 * it, through kernel read/write the PS5's jailbreak hands our payload. A PS4 payload has no such
 * door - measured, four ways, and all shut: mdbg (syscall 573) is EPERM even with full credential
 * escalation, ptrace (26) is EPERM before it even looks the target up, and the jailbreak's own
 * kernel gateway can only ever act on the CALLING process, which for our shop is a system daemon
 * and not the game. So on a PS4 the only place that can write a game's memory is inside the game,
 * and this is the smallest thing that can live there.
 *
 * ================= WHY THERE IS NO SOCKET IN THIS FILE ANY MORE =================
 *
 * The previous editions of this agent served memory over a loopback socket, and every one of them
 * BROKE GAMES - first a freeze on the "Please wait..." screen, and finally badly enough that the
 * console had to be rebooted and re-jailbroken. Each round removed a suspect (raw printf, which can
 * block on a stdout the game never drains; work on the load path; a byte patch applied to the
 * module after it was signed) and the game still broke.
 *
 * What was left was a comparison. Every operation this agent performs is now one that a SHIPPING
 * plugin on this console is proof is safe:
 *     creating a thread in plugin_load and sleeping first  - frame_logger does exactly this
 *     enumerating modules and reading segment info         - game_patch does this at load
 *     reading and writing files                            - game_patch and frame_logger both
 *     sceKernelMprotect + memcpy to patch the game          - game_patch's whole purpose
 * and the ONE thing no working plugin does, anywhere, was the thing only we did: open a LISTENING
 * SOCKET inside a game process. So it is gone. The shop and the agent now talk through files, which
 * is a mechanism the console already demonstrates is safe in this exact context.
 *
 * ================= THE FILE CHANNEL =================
 *
 * One request at a time, in a directory both sides can reach:
 *     cmd.bin   written by the SHOP, read and then deleted by this agent
 *     res.bin   written by this AGENT, read and then deleted by the shop
 * Both are written to a ".tmp" beside them and renamed into place, so neither side can ever read a
 * half-written file - the same discipline the rest of this project uses for every file something
 * else might open early.
 *
 *     request   "PMSC" seq:u32 op:u8 pad:u8[3] addr:u64 len:u32  [len bytes, WRITE only]
 *     response  "PMSR" seq:u32 st:u8  pad:u8[3] len:u32          [len bytes, READ/STATUS only]
 *
 * `seq` is echoed back so a reply can never be mistaken for the answer to a different question -
 * the failure a bare file drop would otherwise have. `len` is the length the request is ABOUT: for
 * a read nothing follows the header, for a write exactly len bytes do. (An earlier socket edition
 * put the OUTGOING byte count there, which for a read is zero, and so not one read could ever have
 * succeeded while looking exactly like "no game is running". Both ends agree with this comment.)
 *
 * NOTHING HERE KNOWS WHAT A CHEAT IS. It answers four questions - are you there, what process is
 * this, read these bytes, write these bytes - and every decision about files, formats, titles and
 * offsets stays in the shop where it can be tested without a console. That line is drawn there
 * because every line of this file runs inside somebody's game.
 */

#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <fcntl.h>          /* MACROS ONLY - O_RDONLY/O_WRONLY/O_CREAT/O_TRUNC/O_APPEND, SEEK_END.
                               Including this adds no import; it used to be avoided because the raw
                               open() it declares WAS one, and nothing here calls that any more. */

#include <orbis/libkernel.h>

/* THE IMPORT TABLES, MEASURED - not inferred. An earlier version of this comment claimed the
   plugins this console loads cleanly "do their file work with fopen and friends" and that our raw
   POSIX calls were imports "neither of them imports". BOTH HALVES WERE WRONG. Here is what the
   three modules actually import, decoded from each one's DT_SCE_SYMTAB:

     plugin_template - libkernel: sceKernelSendNotificationRequest. libSceLibcInternal: memset,
       printf, snprintf, strcpy, vsprintf. No file work at all.

     game_patch - libkernel: __error, pthread_getspecific, pthread_key_create, pthread_once,
       pthread_setspecific, read, sceKernelChmod, sceKernelClose, sceKernelGetModuleInfo,
       sceKernelGetModuleList, sceKernelLseek, sceKernelMkdir, sceKernelOpen, sceKernelRead,
       sceKernelSendNotificationRequest, sceKernelWrite, write. libSceLibcInternal: 31 symbols
       including fprintf, getc, putc, the stderr FILE object, and malloc/calloc/free/realloc.
       So it does its file work with sceKernelOpen/Read/Write/Close/Lseek/Mkdir and raw read/write,
       NOT with fopen - while still proving the FILE machinery resolves inside a game.

     ours, now: libSceLibcInternal - memcpy, memset, snprintf. libkernel - sceKernelOpen,
       sceKernelRead, sceKernelWrite, sceKernelClose, sceKernelLseek, sceKernelGetModuleList,
       sceKernelGetModuleInfo, sceKernelMprotect, sceKernelUsleep, scePthreadCreate,
       scePthreadDetach, getpid, sceKernelGetAppInfo.

   ## MEASURED ON THE CONSOLE, AND IT ENDED THE INVESTIGATION

   * a build whose plugin_load sets a global and returns, with ZERO undefined symbols, LOADED AND
     PLAYED. GoldHEN announced it on screen and Dark Souls II ran. So the module format, the signing,
     the crt, the paid, the SDK version and the build recipe are all FINE. Stop re-checking them, and
     nothing from GoldHEN's SDK is needed.
   * the SAME build plus three imports - fopen, fwrite, fclose - and a call to them in plugin_load
     CRASHED the game.
   * the file-channel agent, which called the same stdio from a worker thread six seconds in, also
     crashed the game.

   So LIBC STDIO IS THE THING THAT BREAKS THIS, on the load path or later. What misled us for weeks:
   game_patch imports fprintf, getc, putc and the stderr FILE object, which reads like "the FILE
   machinery resolves" - but game_patch does its actual file work with sceKernelOpen/Read/Write/
   Close/Lseek/Mkdir and raw read/write, and never calls fopen. "FILE symbols bind" and "fopen is
   safe inside a game" are not the same claim, and only the first one was ever evidenced.

   THE RULE THIS FILE NOW FOLLOWS: every file operation is a sceKernel* call. No stdio, and
   `tools/test_agent_protocol.py` fails the build if any reappears. printf was never the difference
   (both working plugins import it), but it stays out too - it can block on a stdout no game drains. */

/* errno, the FreeBSD way. <errno.h> resolves to glibc's __errno_location, which is in no PS4
   library - the link fails on it. FreeBSD (and so the PS4) exports __error(). */
/* Declared rather than pulled in from <unistd.h>: this file deliberately keeps its include set
   (and so its import set) as small as the plugins this console is known to load. getpid is used
   only to report the process in the status document. */
extern int getpid(void);

/* DECLARED, NOT INCLUDED. <stdio.h> would bring the whole FILE machinery into scope, and libc stdio
   is the one thing measured to crash games from inside them - so the single libc formatting call this
   file uses is declared by hand instead, exactly as getpid and __error are above. An include does not
   create an import (only a CALL does), but it does create the opportunity for one, and this file has
   paid enough for that. */
extern int snprintf(char *, size_t, const char *, ...);

extern int *__error(void);
#define pms_errno (*__error())

/* The loader looks these up by name, so they must be in the dynamic table. attr_public is exactly
   what a plugin needs it to be - default visibility, nothing more magic. */
#define attr_public        __attribute__((visibility("default")))
#define attr_module_hidden __attribute__((weak)) __attribute__((visibility("hidden")))

/* THE NAME ANYTHING ON GOLDHEN'S SIDE WILL PRINT. Its loader puts this in its own load banner
   and in klog, and "pms_agent" told the owner nothing about whose code it was. */
attr_public const char *g_pluginName = "PKG MUTANT SHOP Cheat Engine";
attr_public const char *g_pluginDesc = "PKG MUTANT SHOP in-game agent";
attr_public const char *g_pluginAuth = "PKG MUTANT SHOP";
attr_public uint32_t    g_pluginVersion = 0x00000400;   /* 4.00 - the file channel */

/* HOW LONG THE WORKER WAITS BEFORE TOUCHING ANYTHING, in microseconds.
   plugin_load runs on the game's own load path while the screen still says "Please wait...".
   Nothing of ours should be competing with that, and the shop does not need us until somebody
   opens the mods panel, which is far later. The PC test builds this out (nothing to wait for). */
#ifndef PMS_AGENT_START_DELAY_US
#define PMS_AGENT_START_DELAY_US (6 * 1000 * 1000)
#endif

/* HOW OFTEN THE WORKER LOOKS FOR A REQUEST - two rates, because one cannot be right for both cases.
   Measured on the console at a fixed 500 ms: one round trip averaged 495 ms, so a cheat toggle (three
   round trips) took 1.5 seconds and opening the mods panel took a second. Nothing else was slow -
   /api/health answers in 17 ms - so this interval was the entire latency budget.
   Polling fast all the time is the wrong fix: this loop lives inside somebody's game for as long as
   they play, and 40 file checks a second for ever is a real cost to answer a question nobody is asking.
   So the rate follows use. Any request makes it fast for the next few seconds; the panel polls while it
   is open, which keeps that window alive for as long as somebody is actually cheating. */
#ifndef PMS_AGENT_POLL_US
#define PMS_AGENT_POLL_US (500 * 1000)          /* idle: nobody has asked for a while */
#endif
#ifndef PMS_AGENT_BUSY_US
#define PMS_AGENT_BUSY_US (25 * 1000)           /* busy: a session is in progress */
#endif
/* How long a single request keeps the fast rate alive. Comfortably longer than the panel's own refresh
   so that an open panel never drops back to the idle rate between polls. */
#define PMS_BUSY_WINDOW_US (6ULL * 1000 * 1000)
/* And how often alive.bin goes out, whatever the poll rate is doing. The shop treats anything older
   than AGENT_ALIVE_MAX_AGE (8 s) as gone, so 2 s leaves room for three missed beats. */
#define PMS_BEAT_US        (2ULL * 1000 * 1000)

/* A cap, not a buffer to tune. Cheats write a handful of bytes; a request for more than this is a
   misunderstanding on the shop's side and the game should not be asked to allocate for it.

   IT WAS 64 KiB AND THAT WAS INDEFENSIBLE. Two static buffers are sized from it - g_io and
   serve_one's out - so the module reserved 128 KiB of zero-filled memory inside somebody's game for
   a path that never moves more than 256 bytes: /api/mem/read clamps len to 256 and every
   cheat_core.h caller passes a cheat's own byte-string length. The 12 plugins this console loads
   cleanly keep .bss under about 1.1 KB; ours declared 138,848.

   HONESTY ABOUT WHY THIS CHANGED: not because it is the load failure. It is not. The MINIMAL
   diagnostic build has no .bss at all - measured, memsz equals filesz in every PT_LOAD - and it
   still broke the game, so an oversized memory image cannot be the cause. This is one buffer being
   the size its job needs, and build-wsl.sh now prints .bss on every build so the next person sees
   the number instead of inferring it.

   There is a floor: OP_STATUS builds a status line in char info[420] and memcpy's up to all 420
   bytes into out + RES_HDR, so a chunk smaller than that would overrun a buffer inside a game. The
   assert is here rather than in a comment because a comment cannot fail a build. */
#define PMS_MAX_CHUNK (4 * 1024)
_Static_assert(PMS_MAX_CHUNK >= 420,
               "OP_STATUS memcpy's up to sizeof(info) = 420 bytes into out + RES_HDR");

#define PMS_REQ_MAGIC "PMSC"
#define PMS_RES_MAGIC "PMSR"

enum { OP_PING = 1, OP_STATUS = 2, OP_READ = 3, OP_WRITE = 4 };
enum {
    ST_OK       = 0,
    ST_BADREQ   = 1,
    ST_TOOBIG   = 2,
    ST_UNMAPPED = 3,   /* not inside this process's own modules - refused, nothing touched */
    ST_PROTFAIL = 4,   /* the page would not become writable */
    ST_NOINFO   = 5,
    ST_FOREIGN  = 6    /* mapped, and ours to read - but not the executable, so not ours to write */
};

/* WHERE THE TWO SIDES MEET. The shop's own data directory is first choice; the jailbreak's tree is
   the fallback, because plugins are demonstrably able to write there (its own plugins do). Which
   one is in use is decided once, at startup, by actually creating and writing - never assumed. */
static const char *const PMS_DIRS[] = {
#ifdef PMS_AGENT_DIR_OVERRIDE
    /* The PC test builds this in and points it at a temporary directory - there is no /data on a
       PC. It exists only so the file protocol can be exercised end to end off-console; no console
       build ever defines it. */
    PMS_AGENT_DIR_OVERRIDE,
#endif
    "/data/pkg-mutant-shop/agent",
    "/data/GoldHEN/pms-agent",
    NULL
};
static char g_dir[128];
/* No .tmp of our own any more - nothing here writes one. The SHOP still writes cmd.tmp and renames
   it over cmd.bin, which is its business and costs us nothing. */
static char g_cmd[160], g_res[160], g_alive[160];

/* ---- diagnostics, compiled in only when asked -------------------------------------------------
 * Never printf: libc printf can block on a stdout the game does not drain, and that is a freeze on
 * the load path. A short line appended to a real file cannot block that way. Off by default. */
#ifdef PMS_AGENT_TRACE
static void pms_trace(const char *s) {
    char p[200];
    int k = snprintf(p, sizeof(p), "%s/trace.txt", g_dir[0] ? g_dir : "/data/GoldHEN");
    (void)k;
    int fd = sceKernelOpen(p, O_WRONLY | O_CREAT | O_APPEND, 0777);
    if (fd < 0) return;
    /* strlen is not imported here - the caller always passes a literal, and snprintf gives us the
       length of one without another import. */
    char line[200];
    int n = snprintf(line, sizeof(line), "%s\n", s);
    if (n > 0) (void)sceKernelWrite(fd, line, (size_t)(n < (int)sizeof(line) ? n : (int)sizeof(line) - 1));
    sceKernelClose(fd);
}
#else
#define pms_trace(s) ((void)0)
#endif

/* ---- where we are, and what of it we may touch ---------------------------------------------- */

/* app_info's title id is at offset 16, 10 bytes. MEASURED on this firmware, and confirmed a second
   way by OrbisAppInfo's own field layout in the toolchain header. 0x100 is far larger than either
   candidate layout, so the call cannot write past this buffer whichever one is right. */
typedef struct app_info_probe { unsigned char raw[0x100]; } app_info_probe_t;

static char g_title[16];

/* Written by the PMS_AGENT_NULL build and by nothing else. It exists so that build's plugin_load
   has a side effect the compiler must keep, without calling anything. */
volatile int g_null_probe;

/* Every mapped segment of every module in this process. A cheat offset is always module-relative,
   so this is both the set of addresses worth allowing and a far stronger test than "the kernel has
   heard of this address" - which says nothing about whether a read will fault. */
#define PMS_MAX_SEG 128
/* prot is carried because the SDK already hands it to us and we used to throw it away: after a
   patch the page has to go back to the protection the game gave it. */
typedef struct { uint64_t lo, hi; int32_t prot; } pms_seg_t;
static pms_seg_t g_seg[PMS_MAX_SEG];

/* WHERE A WRITE IS ALLOWED, which is a strictly smaller set than where a READ is allowed: the
   segments of eboot.bin only. Every cheat offset in the library is relative to the executable
   ("process": "eboot.bin" in the files themselves), so a write landing in a shared library is a
   cheat aimed at the wrong module - and eboot.bin is also the one module that cannot be unloaded
   while the process lives, which removes the stale-map class of fault from the write path
   altogether. VALUES, never indices into g_seg: scan_modules rebuilds g_seg from zero on every
   call while g_base deliberately survives a rescan that did not find eboot.bin, so a stored index
   would later point at whatever module happens to occupy that slot - and authorise a write into
   it. Its own array also keeps it clear of the PMS_MAX_SEG ceiling that g_seg fills under. */
#define PMS_MAX_BASE_SEG 4
static struct { uint64_t lo, hi; } g_base_seg[PMS_MAX_BASE_SEG];
static int g_nbase_seg;
static int g_nseg;

static uint64_t g_base;
static uint32_t g_base_size;
static char     g_modname[64];
static size_t   g_nmod;

static int this_process_is_a_game(void) {
    app_info_probe_t info;
    memset(&info, 0, sizeof(info));
    if (sceKernelGetAppInfo(getpid(), (OrbisAppInfo *)&info) != 0) return 0;
    memcpy(g_title, info.raw + 16, 10);
    g_title[10] = 0;
    for (int i = 0; g_title[i]; i++)
        if (g_title[i] < 32 || g_title[i] > 126) { g_title[i] = 0; break; }
    /* A game is CUSA or PPSA. NPXS is a system app - the shell, the store, the browser - and none
       of those has cheats. A plugin listed under [default] loads into all of them, so anything
       that is not a game leaves immediately without starting a thread. */
    return (!strncmp(g_title, "CUSA", 4) || !strncmp(g_title, "PPSA", 4));
}

static void scan_modules(void) {
    enum { N = 512 };
    static OrbisKernelModule handles[N];      /* static: 2 KB has no business on a thread stack */
    OrbisKernelModuleInfo mi;
    size_t got = 0;

    int rc = sceKernelGetModuleList(handles, (size_t)N, &got);
    if (rc != 0 || !got) return;
    if (got > (size_t)N) got = (size_t)N;

    /* THE SEGMENT MAP IS REBUILT, NEVER ADDED TO. Keeping a segment that belongs to a module the
       game has since unloaded would let a read through to memory that is no longer mapped, and that
       fault is fatal inside the game - the exact thing this map exists to prevent.
       THE BASE survives a rescan that does not find eboot.bin: it cannot move while the process
       lives, and reporting 0 would tell the shop "no game" in the middle of applying a cheat. */
    uint64_t keep_base = g_base;
    uint32_t keep_size = g_base_size;
    char     keep_name[64];
    snprintf(keep_name, sizeof(keep_name), "%s", g_modname);
    /* The write map is preserved with the base for the same reason and by the same rule: base and
       the segments that define it must never disagree, or a write could be authorised against a
       base that no longer belongs to it. */
    int keep_nbase = g_nbase_seg;
    static struct { uint64_t lo, hi; } keep_base_seg[PMS_MAX_BASE_SEG];
    memcpy(keep_base_seg, g_base_seg, sizeof(keep_base_seg));

    g_nseg = 0;
    g_nbase_seg = 0;
    g_base = 0;
    g_base_size = 0;
    g_modname[0] = 0;
    g_nmod = got;

    for (size_t i = 0; i < got; i++) {
        memset(&mi, 0, sizeof(mi));
        /* MANDATORY. OrbisKernelModuleInfo begins with its own size and the call refuses a struct
           that does not declare it. Leaving it zero is why an earlier build reported base=0x0. */
        mi.size = sizeof(mi);
        if (sceKernelGetModuleInfo(handles[i], &mi) != 0) continue;

        unsigned segs = mi.segmentCount;
        if (segs > 4) segs = 4;               /* the struct holds four, whatever it reports */
        for (unsigned s = 0; s < segs && g_nseg < PMS_MAX_SEG; s++) {
            uint64_t lo = (uint64_t)mi.segmentInfo[s].address;
            uint64_t len = (uint64_t)mi.segmentInfo[s].size;
            if (!lo || !len) continue;
            g_seg[g_nseg].lo = lo;
            g_seg[g_nseg].hi = lo + len;      /* exclusive */
            g_seg[g_nseg].prot = mi.segmentInfo[s].prot;
            g_nseg++;
        }

        /* The executable, by name. Our cheat files say so themselves: "process": "eboot.bin".
           NO FALLBACK TO "THE FIRST MODULE": applying a cheat offset relative to some shared
           library would put every write somewhere arbitrary, and a wrong base cannot be told from
           a right one by anybody downstream. Reporting no base lets the shop refuse. */
        if (!strcmp(mi.name, "eboot.bin") && mi.segmentCount) {
            g_base = (uint64_t)mi.segmentInfo[0].address;
            g_base_size = (uint32_t)mi.segmentInfo[0].size;
            snprintf(g_modname, sizeof(g_modname), "%.63s", mi.name);
            g_nbase_seg = 0;
            for (unsigned b = 0; b < segs && g_nbase_seg < PMS_MAX_BASE_SEG; b++) {
                uint64_t blo = (uint64_t)mi.segmentInfo[b].address;
                uint64_t blen = (uint64_t)mi.segmentInfo[b].size;
                if (!blo || !blen) continue;
                g_base_seg[g_nbase_seg].lo = blo;
                g_base_seg[g_nbase_seg].hi = blo + blen;
                g_nbase_seg++;
            }
        }
    }

    if (!g_base && keep_base) {
        g_base = keep_base;
        g_base_size = keep_size;
        snprintf(g_modname, sizeof(g_modname), "%s", keep_name);
        g_nbase_seg = keep_nbase;
        memcpy(g_base_seg, keep_base_seg, sizeof(keep_base_seg));
    }
}

/* Has this process loaded or dropped a module since the last scan? One syscall, not a walk - which
   is what makes it affordable on the path it is used from, after an address has ALREADY been
   refused, because a cheat file written for a different build misses every entry. */
static int modules_changed(void) {
    enum { N = 512 };
    static OrbisKernelModule probe[N];
    size_t got = 0;
    if (sceKernelGetModuleList(probe, (size_t)N, &got) != 0 || !got) return 0;
    if (got > (size_t)N) got = (size_t)N;
    return got != g_nmod;
}

/* Returns the containing entry as i + 1, so 0 still means "not ours" and every `!range_is_ours(..)`
   caller keeps working. It must NOT return a bare index: entry 0 is eboot.bin's own .text, which is
   the main cheat target, and a bare 0 there would read as a refusal. */
static int in_segments(uint64_t addr, uint64_t end) {
    for (int i = 0; i < g_nseg; i++)
        if (addr >= g_seg[i].lo && end <= g_seg[i].hi) return i + 1;
    return 0;
}

static int range_is_ours(uint64_t addr, uint32_t len) {
    if (!addr || !len) return 0;
    uint64_t end = addr + len;
    if (end < addr) return 0;                 /* wrapped */
    int hit = in_segments(addr, end);
    if (hit) return hit;
    /* A MISS IS NOT YET A NO. This map was built before the game finished loading its own
       libraries; an address inside one that arrived later is a good address against a stale map.
       Rebuilding only when the module COUNT changed keeps the common case - a bad offset - cheap. */
    if (!modules_changed()) return 0;
    scan_modules();
    return in_segments(addr, end);
}

/* A WRITE HAS TO CLEAR A SECOND BAR. range_is_ours runs first so its rescan-on-miss retry still
   happens (otherwise the first write after the map went stale would be refused with no retry), and
   then the range must also sit inside eboot.bin. Returns the g_seg entry (i + 1) when the write is
   allowed, 0 when the address is not mapped at all, and -1 when it is mapped but belongs to some
   other module - a distinction worth keeping, because the two mean very different things about the
   cheat file that asked for it. */
static int range_is_writable(uint64_t addr, uint32_t len) {
    int hit = range_is_ours(addr, len);
    if (!hit) return 0;
    uint64_t end = addr + len;
    for (int i = 0; i < g_nbase_seg; i++)
        if (addr >= g_base_seg[i].lo && end <= g_base_seg[i].hi) return hit;
    return -1;
}

/* ---- files ------------------------------------------------------------------------------------ */

/* ---- files: sceKernel* ONLY ---------------------------------------------------------------------
   sceKernelRead and sceKernelWrite are declared returning size_t, so a failure comes back as a
   negative errno reinterpreted as a huge value. Casting to long makes it negative again, which is
   why every loop below tests `<= 0` on a long rather than on the returned type. */
static int read_whole(const char *path, unsigned char *buf, size_t cap, size_t *out_n) {
    int fd = sceKernelOpen(path, O_RDONLY, 0);
    if (fd < 0) return -1;
    size_t n = 0;
    while (n < cap) {
        long r = (long)sceKernelRead(fd, buf + n, cap - n);
        if (r <= 0) break;
        n += (size_t)r;
    }
    sceKernelClose(fd);
    *out_n = n;
    return 0;
}

/* How big is it - or -1 if it is not there. Answered with lseek rather than stat, because stat is an
   import no plugin on this console demonstrates and the length is the only thing anybody wants. */
static long file_size(const char *path) {
    int fd = sceKernelOpen(path, O_RDONLY, 0);
    if (fd < 0) return -1;
    long sz = (long)sceKernelLseek(fd, 0, SEEK_END);
    sceKernelClose(fd);
    return sz;
}

/* EMPTY A FILE WITHOUT DELETING IT. sceKernelUnlink exists, but game_patch does not import it and
   nothing else on this console demonstrates it, so O_TRUNC does the same job with a call we already
   rely on. A zero-length cmd.bin means exactly what a missing one did: no request. */
static void file_empty(const char *path) {
    int fd = sceKernelOpen(path, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (fd >= 0) sceKernelClose(fd);
}

/* WRITTEN STRAIGHT INTO PLACE - no .tmp, and no rename.
   rename() was the one import in this module that nothing on this console demonstrates, and it only
   existed to make a reader unable to see a half-written file. It is not needed for that: the reader
   validates the response by the LENGTH DECLARED IN ITS OWN HEADER, so a short file is rejected as
   an error and never mistaken for a value, and the shop's side simply keeps waiting for the rest.
   That is a property of the protocol, not of the filesystem, which makes it the stronger guarantee
   of the two. */
static int write_whole(const char *path, const unsigned char *buf, size_t n) {
    int fd = sceKernelOpen(path, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (fd < 0) return -1;
    size_t done = 0;
    while (done < n) {
        long w = (long)sceKernelWrite(fd, buf + done, n - done);
        if (w <= 0) break;
        done += (size_t)w;
    }
    sceKernelClose(fd);
    return done == n ? 0 : -1;
}

/* SAY SO WHEN THERE IS NOWHERE TO TALK. Not behind PMS_AGENT_TRACE: a missing alive.bin is
   byte-identical to "the module never loaded", and that is the exact distinction this whole
   investigation turns on - so the one case where the agent is alive but mute has to leave a mark in
   the shipping build. /data/GoldHEN is confirmed present on the console, and a file there needs no
   directory created. sizeof - 1 rather than strlen, because strlen is an import this module does not
   carry and is not about to add while a load failure is unexplained. */
static void pms_mark_no_dir(void) {
    /* The newline is in the literal and the length comes from sizeof, so this needs neither a second
       write call nor strlen - both of which would be imports, and the import set is the whole
       subject of this file. */
    static const char msg[] = "pms agent: no writable channel directory\n";
    (void)write_whole("/data/GoldHEN/pms-agent-nodir.txt",
                      (const unsigned char *)msg, sizeof(msg) - 1);
}

/* Pick the directory we can actually write in, and remember the four paths built from it. */
/* The SHOP creates these directories - it runs as root in the payload and already owns its own
   data folder. All the agent does is find one it can actually write in, and it decides that by
   writing, never by asking. */
static int choose_dir(void) {
    for (int i = 0; PMS_DIRS[i]; i++) {
        char probe[160];
        snprintf(probe, sizeof(probe), "%s/.w", PMS_DIRS[i]);
        /* Decided by actually writing, never by asking. The empty probe file is LEFT BEHIND: there
           is no unlink here (see file_empty) and a zero-byte .w costs nothing. */
        int pf = sceKernelOpen(probe, O_WRONLY | O_CREAT | O_TRUNC, 0777);
        if (pf < 0) continue;
        sceKernelClose(pf);
        snprintf(g_dir, sizeof(g_dir), "%s", PMS_DIRS[i]);
        snprintf(g_cmd,     sizeof(g_cmd),     "%s/cmd.bin", g_dir);

        snprintf(g_res,     sizeof(g_res),     "%s/res.bin", g_dir);

        snprintf(g_alive,   sizeof(g_alive),   "%s/alive.bin", g_dir);
        return 0;
    }
    return -1;
}

/* ---- one request ------------------------------------------------------------------------------ */

/* Laid out explicitly rather than by struct, so there is no question of padding:
     [0..3]   magic
     [4..7]   seq        u32
     [8]      op         u8
     [9..11]  pad
     [12..19] addr       u64
     [20..23] len        u32
   = 24 bytes. The response is magic4 seq4 st1 pad3 len4 = 16 bytes. */
#define REQ_HDR 24
#define RES_HDR 16

static unsigned char g_io[PMS_MAX_CHUNK + REQ_HDR];

static void serve_one(void) {
    size_t n = 0;
    if (read_whole(g_cmd, g_io, sizeof(g_io), &n) != 0) return;
    /* Take the request off the queue immediately: if anything below goes wrong we must not spin on
       the same file for the life of the game. */
    file_empty(g_cmd);
    if (n < REQ_HDR || memcmp(g_io, PMS_REQ_MAGIC, 4) != 0) return;

    uint32_t seq = 0, len = 0;
    uint64_t addr = 0;
    memcpy(&seq,  g_io + 4,  4);
    unsigned char op = g_io[8];
    memcpy(&addr, g_io + 12, 8);
    memcpy(&len,  g_io + 20, 4);

    unsigned char st = ST_OK;
    uint32_t outlen = 0;
    static unsigned char out[PMS_MAX_CHUNK + RES_HDR];
    char info[420];

    if (op == OP_PING) {
        /* nothing to say beyond "yes" */
    } else if (op == OP_STATUS) {
        if (!g_base) scan_modules();          /* a retry costs one call and no risk */
        if (!g_base) {
            st = ST_NOINFO;
        } else {
            int k = snprintf(info, sizeof(info),
                "{\"ok\":true,\"agent\":\"pms\",\"pid\":%d,\"base\":\"0x%llx\",\"size\":%u,"
                "\"module\":\"%s\",\"title_id\":\"%s\",\"segments\":%d,\"ver\":\"%u\"}",
                (int)getpid(), (unsigned long long)g_base, g_base_size,
                g_modname[0] ? g_modname : "?", g_title[0] ? g_title : "",
                g_nseg, (unsigned)g_pluginVersion);
            if (k < 0) k = 0;
            if (k > (int)sizeof(info)) k = (int)sizeof(info);
            outlen = (uint32_t)k;
            memcpy(out + RES_HDR, info, outlen);
        }
    } else if (op == OP_READ) {
        if (len == 0 || len > PMS_MAX_CHUNK)  st = ST_TOOBIG;
        else if (!range_is_ours(addr, len))   st = ST_UNMAPPED;
        else { memcpy(out + RES_HDR, (const void *)addr, len); outlen = len; }
    } else if (op == OP_WRITE) {
        if (len == 0 || len > PMS_MAX_CHUNK)        st = ST_TOOBIG;
        else if (n < (size_t)REQ_HDR + len)         st = ST_BADREQ;   /* payload truncated */
        else {
            int w = range_is_writable(addr, len);
            if (w == 0)      st = ST_UNMAPPED;
            else if (w < 0)  st = ST_FOREIGN;
            else {
                const pms_seg_t *sg = &g_seg[w - 1];
                /* Page-align before asking for write permission: mprotect works in pages, and a
                   range starting mid-page must not shorten the request. 0x4000 is not a guess - it
                   is the alignment the SDK's own link.x gives .text, .data.rel.ro and
                   .data.sce_module_param, and every segment in both plugins this console loads
                   cleanly sits on a multiple of it (0x0/0x4000/0xc000 and 0x0/0x10000/0x1c000).
                   That is what makes rounding the cover outwards safe: no other mapping can begin
                   inside a block this segment already occupies. Rounding the END up is therefore
                   fine even when the segment's own size is not a multiple of 0x4000 - which is the
                   normal case for a final data segment.

                   AND NEITHER END IS CHECKED AGAINST THE SEGMENT. A first version of this refused the
                   write when the rounded-down start fell below the segment's own base, on the
                   reasoning that rounding must not escape the segment. That is wrong for exactly the
                   reason the end check is wrong, and the PC harness proved it within one run: its fake
                   module sits wherever mmap puts it, so on a run where the base was not a multiple of
                   0x4000 every write into the segment's FIRST block was refused with ST_PROTFAIL -
                   and it passed on the previous run, because the address had happened to be aligned.
                   There is nothing to fix by refusing: mprotect works in whole pages and cannot be
                   asked for less, and the range actually WRITTEN is already proved to lie inside
                   eboot.bin by range_is_writable. Only the protection cover is rounded. */
                uint64_t pg = addr & ~((uint64_t)0x3FFF);
                size_t sz = (size_t)((addr + len) - pg);
                sz = (sz + 0x3FFF) & ~((size_t)0x3FFF);
                if (sceKernelMprotect((const void *)pg, sz, VM_PROT_ALL) != 0) st = ST_PROTFAIL;
                else {
                    memcpy((void *)addr, g_io + REQ_HDR, len);
                    /* PUT THE PAGE BACK. Leaving a code page writable for the rest of the session
                       is a change to the game nobody asked for. Best-effort, because the bytes are
                       already written and there is nothing useful to report; and GUARDED, because a
                       prot that came back 0 must not be restored - handing a live code page
                       VM_PROT_NONE would fault the game the next time it ran an instruction there. */
                    if (sg->prot & VM_PROT_READ)
                        (void)sceKernelMprotect((const void *)pg, sz, sg->prot);
                }
            }
        }
    } else {
        st = ST_BADREQ;
    }

    memcpy(out, PMS_RES_MAGIC, 4);
    memcpy(out + 4, &seq, 4);
    out[8] = st;
    out[9] = out[10] = out[11] = 0;
    memcpy(out + 12, &outlen, 4);
    write_whole(g_res, out, (size_t)RES_HDR + outlen);
}

static void *agent_thread(void *arg) {
    (void)arg;

    /* HOLD BACK until the game is past its load screen. Everything below is work an earlier build
       did immediately, and it froze the game. The shop does not need us for minutes. */
    if (PMS_AGENT_START_DELAY_US) sceKernelUsleep(PMS_AGENT_START_DELAY_US);
    pms_trace("thread: awake");

    /* IS THIS EVEN A GAME? This check used to run in plugin_load, on the game's own load path. It
       is here now because it was the last substantive call left there, and plugin_load's only job
       is to return.
       WHY THAT CALL IS SUSPECT, recorded so nobody re-adds it earlier: sceKernelGetAppInfo's NID is
       G-MYv5erXaU and it is in NEITHER plugin this console loads cleanly; GoldHEN's own SDK reaches
       a title id through orbis_syscall(500) instead, deliberately avoiding it; and this repo already
       carries a measured observation about the same call family - server_ps4.c records that when a
       game crashed mid-load, an inline call of this kind never returned and /api/health connected
       and then hung for ever.
       It runs BEFORE choose_dir on purpose: choose_dir writes a probe file, and a plugin listed for
       every title must not create files under /data from inside a system app. */
    if (!this_process_is_a_game()) return NULL;
    pms_trace("thread: title ok");

    /* KEEP LOOKING, do not give up for the life of the process. The old code returned here, which
       killed the worker permanently and silently - and the commonest reason to land here is simply
       that the game started before the shop's payload created the directory, which fixes itself a
       few seconds later. The mark is written once, not once per attempt. */
    for (int tries = 0; choose_dir() != 0; tries++) {
        if (tries == 0) pms_mark_no_dir();
        sceKernelUsleep(PMS_AGENT_POLL_US * 4);
    }
    pms_trace("thread: dir ready");

    /* Anything left from a previous run is not an answer to a question anybody is still asking. */
    file_empty(g_cmd);
    file_empty(g_res);

    scan_modules();
    pms_trace("thread: scanned, serving");

    /* THE HEARTBEAT. Without it the shop has no cheap way to know whether an agent is in the game
       at all, so every panel refresh would have to post a request and wait out the timeout - five
       seconds of nothing, on a route the UI polls. A tiny file whose MODIFICATION TIME both sides
       can read settles it with one stat(): fresh means an agent is alive in a game right now.
       Rewritten every few seconds rather than every poll, because this runs for as long as
       somebody plays. */
    unsigned int beat = 0;
    /* Start idle, and beat immediately: the shop's first question should not have to wait 2 s for a
       heartbeat before it is even willing to ask. */
    unsigned long long since_req_us  = PMS_BUSY_WINDOW_US;
    unsigned long long since_beat_us = PMS_BEAT_US;

    for (;;) {
        if (file_size(g_cmd) > 0) {
            serve_one();
            since_req_us = 0;               /* somebody is using it - stay quick */
        }

        /* TIME-BASED, NOT EVERY NTH ITERATION. At the busy rate an every-4th-beat heartbeat would
           write this file ten times a second inside the game, which would cost more than the latency
           it buys. The elapsed time is accumulated from the sleeps below, so this needs no clock. */
        if (since_beat_us >= PMS_BEAT_US) {
            unsigned char hb[4];
            memcpy(hb, &beat, 4);
            (void)write_whole(g_alive, hb, sizeof(hb));
            beat++;
            since_beat_us = 0;
        }

        unsigned int nap = (since_req_us < PMS_BUSY_WINDOW_US)
                         ? (unsigned int)PMS_AGENT_BUSY_US
                         : (unsigned int)PMS_AGENT_POLL_US;
        sceKernelUsleep(nap);
        since_req_us  += nap;
        since_beat_us += nap;
    }
}

int32_t attr_public plugin_load(int32_t argc, const char *argv[]) {
    (void)argc; (void)argv;

#if defined(PMS_AGENT_NULL)
    /* THE ZERO-IMPORT BUILD. Sets one global and returns. It calls NOTHING, so the module's
     * undefined-symbol list is empty and there is no import left for the loader to fail to bind.
     *
     * Why this experiment exists. Every structural property of this module has been compared against
     * the plugins this console loads cleanly and matches: the signing header field for field, all 8
     * segment entries, the inner ELF type, the 37-entry DYNAMIC table tag for tag, the module param,
     * the paid, the SDK version, the build flags and the crt. What did NOT match is what we IMPORT,
     * and one import in particular - rename, from libkernel - is the only name we carry that nothing
     * on this console demonstrates resolving inside a game.
     *
     * This build removes that whole variable rather than reasoning about it. It is the cheapest
     * remaining question and it has exactly two answers:
     *   the game RUNS   -> the module format is fine and the load failure is about what we import,
     *                      which is then bisected by adding imports back a few at a time.
     *   the game BREAKS -> nothing we can do to our own code will help. The module itself is not
     *                      being accepted, and the only untried variable left is the signing and
     *                      link plumbing - a decision for the owner, not a guess for us. */
    g_null_probe = 1;
    return 0;
#elif defined(PMS_AGENT_MINIMAL)
    /* THE EXPERIMENT THAT FOUND IT. plugin_load writes ONE line and returns - no title check, no
     * thread, no scan, no polling.
     *
     * WHEN THIS USED STDIO it crashed the game, while PMS_AGENT_NULL above - the same shape with no
     * imports at all - loaded and played. Three symbols apart, fopen/fwrite/fclose, and that was the
     * whole answer: libc stdio is what breaks this module inside a game.
     *
     * It now writes through write_whole() like everything else, so it is no longer a stdio test - it
     * is the smallest possible check that sceKernelOpen/Write/Close work from the load path. Keep it
     * that way: if this ever crashes again, the fault is in the kernel file calls or the module, not
     * in our logic, because there is no logic left in it.
     *
     * One earlier version of this comment claimed the missing marker PROVED the module never loaded.
     * That was a non-sequitur - a failed write and a fault inside plugin_load look identical - and it
     * is struck. What the build did establish is that with no thread, no scan, no protocol and no
     * .bss at all (measured: memsz equals filesz in every PT_LOAD), the game still broke, so neither
     * our own logic nor an oversized memory image could be the cause. */
    {
        static const char mark[] = "plugin_load ran\n";
        if (write_whole("/data/pkg-mutant-shop/agent/minimal.txt",
                        (const unsigned char *)mark, sizeof(mark) - 1) != 0)
            (void)write_whole("/data/GoldHEN/pms-minimal.txt",
                              (const unsigned char *)mark, sizeof(mark) - 1);
    }
    return 0;
#else

    /* THIS FUNCTION RUNS ON THE GAME'S OWN LOAD PATH, so its one job is to RETURN FAST and never
     * block. A game frozen on "Please wait..." is this function not returning. Everything that
     * could be slow or go wrong is on our own thread, after a delay, where the worst case is "the
     * cheat does not turn on" and never "the game is stuck".
     *
     * What happens here, and nothing more: create the thread, detach, return. That is the whole
     * shape frame_logger is cited as proving safe.
     *
     * THE NON-GAME CHECK USED TO BE HERE and has moved into the worker. It was the last substantive
     * call left on the load path, and the call it makes - sceKernelGetAppInfo - is in neither plugin
     * this console loads cleanly. The worker runs it before it writes anything, so a plugin listed
     * for every title still leaves system apps completely alone.
     *
     * signal(SIGPIPE, SIG_IGN) USED TO BE HERE AND IS GONE. It existed to survive writing to a
     * socket the shop had already closed - SIGPIPE's default action terminates the process, and
     * the process is the game. There is no socket any more, so nothing here can raise SIGPIPE,
     * and `signal` was an import that neither plugin this console loads cleanly carries. An import
     * the game's libraries cannot resolve fails the load, which is exactly our symptom, so a call
     * that guards against something that can no longer happen is not worth the risk.
     *
     * There is no printf anywhere in this file, no socket anywhere in this file, and no file I/O
     * on this path - all three were suspects while this agent was breaking games. */
    OrbisPthread t;
    if (scePthreadCreate(&t, NULL, agent_thread, NULL, "pms_agent") == 0)
        scePthreadDetach(t);      /* nothing ever joins it; detaching frees the handle cleanly */
    return 0;
#endif
}

int32_t attr_public plugin_unload(int32_t argc, const char *argv[]) {
    (void)argc; (void)argv;
    /* Nothing to undo. This agent holds no game state and has changed nothing that outlives it;
       whatever a cheat wrote is the game's memory now, and the shop is what tracks that. Nothing
       is notified from here or from plugin_load either: a plugin inside a game should be invisible
       unless the shop asks it for something. */
    return 0;
}

int32_t attr_module_hidden module_start(int64_t argc, const void *args) { (void)argc; (void)args; return 0; }
int32_t attr_module_hidden module_stop(int64_t argc, const void *args)  { (void)argc; (void)args; return 0; }

/* PKG MUTANT SHOP - the PS4 on-console payload.
 *
 * WHAT THIS IS. The PS5 half of this app is ps5-app/onconsole/server.c: a payload that serves the
 * shop's web UI and a JSON API on :8710 and installs packages. This is the same idea for a
 * jailbroken PS4 (FW 13.52, GoldHEN). It is a SEPARATE BINARY - a PS5 prospero ELF simply does not
 * run on a PS4 (measured: GoldHEN accepts it, reports "launched successfully", and nothing
 * happens) - but it deliberately speaks THE SAME HTTP API, so the one web UI and the one PC
 * companion drive either console without caring which is on the other end.
 *
 * WHY IT IS A SEPARATE FILE RATHER THAN #ifdefs IN server.c. The PS5 file is shipping, dense, and
 * full of hard-won facts whose comments record what each arrangement cost to learn. Interleaving a
 * second console's kernel work into it is how you break a working app. Nothing here touches it.
 * The one piece borrowed is the SQLite reader, copied verbatim into sqmini.h (see that file).
 *
 * WHAT IS DIFFERENT FROM THE PS5, AND WHY:
 *
 *   Install. The PS5 lane spawns a helper process that calls sceAppInstUtilInstallByPackage,
 *   because on PS5 that call fails from an injected payload. That function DOES NOT EXIST on PS4.
 *   The PS4 route is BGFT: register a download task pointing at the companion's HTTP URL and start
 *   it; the console downloads and installs it itself, with its own progress and its own toasts.
 *   That is the same shape as the PS5 lane from the companion's side - we serve the bytes, the
 *   console pulls them - which is why the API can stay the same. See bgft.h for where every
 *   signature came from; none of it is guessed.
 *
 *   No kernel read/write. GoldHEN injects us into a process it has already jailbroken, so we start
 *   as root - but it provides no kexec syscall, so the SDK's kernel helpers are unavailable (see
 *   sdk-goldhen.patch.py). Everything here is ordinary userland: sockets, libc, and sce* calls.
 *   The consequence is that the cheat/patch engine, which writes another process's memory through
 *   a page-table walk, has NO PS4 equivalent yet and is not pretended at: the routes answer a
 *   plain "not available on this console" rather than silently doing nothing.
 *
 *   No backups/mount lane. A PS4 game is a PKG. There is no ShadowMount, no .ffpfsc, and no
 *   "runs in place from a drive" concept, so none of that exists here.
 *
 * WHAT IT SHARES WITH THE PS5 BUILD: the web UI (same web/ bundle, same generator), the API route
 * names, the file API so the companion can move files over HTTP instead of FTP, the app.db-derived
 * library, the icon server, and the notification style.
 */

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <errno.h>
#include <pthread.h>
#include <dlfcn.h>
#include <time.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <netinet/tcp.h>     /* TCP_NODELAY on the bulk streams */
#include <arpa/inet.h>
#include <signal.h>
#include <ps4/klog.h>

#include <sys/ptrace.h>
#include <sys/sysctl.h>
#include <sys/user.h>
#include "sqmini.h"
#include "bgft.h"
#include "web_bundle.h"
/* THE PACKAGE THIS ELF INSTALLS - except in the LITE build, which is the copy that travels
   INSIDE that package so the home-screen app can start the shop by itself. Lite is how the
   cycle is broken: package carries the lite payload, full payload carries the package. Three
   stages, no growth, and the only thing lite gives up is installing the package - which the app
   carrying it has by definition already been installed for. See ps4-app/build-all-wsl.sh. */
#ifndef PMS_LITE
#include "tile_bundle.h"
#endif
/* The in-game agent goes in BOTH builds. Unlike the home-screen package - which lite deliberately
   gives up, because the app carrying lite has by definition already been installed - a shop that
   cannot put the agent on the console is a shop where cheats silently do not work, and lite is
   exactly the copy that runs when the owner presses the icon. */
/* The in-game agent travels inside this ELF and is written to the console on boot (unlisted -
   see agent_deploy). It is load-safe now: plugin_load returns instantly with no blocking work, so
   it cannot freeze a game. tools/test_agent_protocol.py compiles it and proves that on a PC. */
#include "agent_bundle.h"
/* The owner's PS4 payloads. In BOTH builds for the same reason the agent is: lite is the copy that
   runs when the icon is pressed, and a panel that cannot send ftpsrv from the icon is a panel that
   only works when a PC is already on - which is the whole thing this is here to avoid. 266 KB. */
#include "payload_bundle_ps4.h"

#ifndef PORT
#define PORT 8710
#endif
#define SHOP_VERSION "3.97.1"

/* The largest POST body this console will take. Bodies here are JSON of a few hundred bytes; the
   cap exists only so a hostile Content-Length cannot ask for a gigabyte of heap. */
#define POST_BODY_MAX (256 * 1024)

/* HOW MUCH THIS CONSOLE MOVES PER SYSCALL ON A BULK PATH.
 *
 * Every bulk loop here used a 64 KB buffer ON THE STACK. Measured on the PS5, which had exactly the
 * same number, raising it to 1 MB took a 128 MB write from 42.7 MB/s to 78.6 - the cost was purely
 * the sixteen-fold difference in read()/write() calls for the same bytes. The PS4 shows the same
 * shape: its throughput rose with the SENDER's chunk size (46.9 MB/s at 64 KB, 59.2 at 4 MB), which
 * is what a syscall-bound path looks like from outside.
 *
 * BUT THIS CONSOLE CANNOT SIMPLY BE GIVEN A MEGABYTE. Its threads run on 256 KB stacks, so a 1 MB
 * stack array is not an option at all, and its heap is genuinely tight - a 256 KB allocation for the
 * library JSON has failed outright on this hardware before. So: ask for the big buffer, and fall
 * back to the 64 KB stack buffer that has always worked when the allocation is refused. Nothing gets
 * slower than it was, and nothing can fail that did not fail before. */
/* ilog() is defined further down with the rest of the logging; bulk_buf_get() below wants it, and
   calling it without a declaration makes the compiler invent a non-static one that then clashes. */
static void ilog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));

#define BULK_BUF_BYTES (1 << 20)
#define BULK_BUF_SMALL 65536

/* Takes the address of a 64 KB stack array to fall back to, and reports the size actually obtained.
   Pair every call with bulk_buf_free().

   A LADDER, BECAUSE THE FIRST RUNG IS NOT ALWAYS THERE. Measured on the owner's PS4 2026-10-02: a
   128 MB upload ran at a median 76.0 MB/s immediately after the payload was loaded and at 53.5 an
   hour later, with the same build - and 53.5 is the 51.6 this console did before any of this, which
   is what falling back to 64 KB looks like from outside. This console's heap is genuinely tight and
   fragments as it runs; a 256 KB allocation for the library JSON has failed outright here before.
   So ask for a megabyte, then for the 256 KB that /pkgfile/ has always allocated successfully on
   this hardware, and only then give up and use the stack. The size actually obtained is logged once
   per change, because a silent fallback is exactly what made this look like a regression. */
static char *bulk_buf_get(char *fallback, size_t *out_sz) {
    static size_t last_logged = 0;
    static const size_t LADDER[] = { BULK_BUF_BYTES, 256 * 1024, 0 };
    for (int i = 0; LADDER[i]; i++) {
        char *b = (char *)malloc(LADDER[i]);
        if (b) {
            *out_sz = LADDER[i];
            if (last_logged != LADDER[i]) {
                last_logged = LADDER[i];
                ilog("bulk transfers are using a %zu KB buffer", LADDER[i] / 1024);
            }
            return b;
        }
    }
    *out_sz = BULK_BUF_SMALL;
    if (last_logged != BULK_BUF_SMALL) {
        last_logged = BULK_BUF_SMALL;
        ilog("bulk transfers fell back to the %d KB stack buffer - the heap is tight",
             BULK_BUF_SMALL / 1024);
    }
    return fallback;                       /* tight on memory: exactly the old behaviour */
}

static void bulk_buf_free(char *b, char *fallback) {
    if (b && b != fallback) free(b);
}

/* How long a remembered answer about what the console has installed stays good for. Defined here
   rather than beside the cache itself because the install paths - which are earlier in the file -
   have to drop that cache the moment they change anything. */
#define TITLES_TTL_MS 5000
static void titles_cache_drop(void);

/* WHEN THIS PROCESS STARTED SERVING, so "did the shop restart?" is a question with an answer.
 * The PS4 shop lives inside a shared system daemon rather than a process of its own, and the
 * reasonable worry is that something - closing the browser, launching a game, resting the console -
 * quietly takes it down and the port with it. Reported by /api/health as `uptime_s`, so anyone can
 * tell a shop that has been up for an hour from one that restarted thirty seconds ago without
 * reading a log over FTP. It is also the honest way to answer that worry: measured, not assumed. */
static long long g_boot_ms = 0;
/* Requests answered since this process started serving. Reported beside uptime_s: a shop that has
   served thousands of requests and been up for an hour is provably the same one that was up
   before, which is what makes "it closed with the browser" answerable. */
static volatile long long g_conns_served = 0;

#define SHOP_DATA_DIR  "/data/pkg-mutant-shop"
#define PB_DIR         "/data/pkg-mutant-shop/payloads"
/* SEEDED ONCE, KEPT FOR EVER. The PS4's homebrew packages are 87 MB - Itemzflow and PS4-Xplorer -
   and this payload is injected into a shared system daemon, so they follow the cheat library's rule
   rather than the agent's: they live on disk and are installed from there. The PC copies them in
   the first time the two meet, and after that this console needs nobody. */
#define HB_DIR         "/data/pkg-mutant-shop/homebrews"
/* THE FOLDER EVERY OTHER GUIDE TELLS PEOPLE TO USE. GoldHEN's BinLoader takes an ELF over :9090,
   and by convention people keep the ones they load in /data/payloads - so that is where somebody
   installing this app from a USB stick or over FTP will put our ELF, with no PC involved at all.
   We create it and mirror our bundled payloads into it; we never load from it. */
#define GH_PAYLOAD_DIR "/data/payloads"

/* THE CHEAT LIBRARY, in the same places the PS5 keeps it. Same names, same layout, deliberately:
   cheat_core.h is the PS5's own code and reads these, and a PS4 that filed its cheats somewhere
   else would need a second copy of every path-building line in it for no gain at all. */
#define CHEAT_ROOT        SHOP_DATA_DIR "/cheats"
#define CHEAT_JSON_DIR    CHEAT_ROOT "/json"
#define CHEAT_SHN_DIR     CHEAT_ROOT "/shn"
#define CHEAT_MC4_DIR     CHEAT_ROOT "/mc4"
#define CHEAT_PATCH_DIR   CHEAT_ROOT "/patches"
#define CHEAT_INBOX_DIR   CHEAT_ROOT "/incoming"
#define WEB_ROOT       SHOP_DATA_DIR "/web"
#define INSTALL_LOG    SHOP_DATA_DIR "/install.log"
#define APP_DB_PATH    "/system_data/priv/mms/app.db"
/* WHAT THE CONSOLE'S CONTENT LOOKS LIKE RIGHT NOW, in one short string.
 *
 * app.db holds the installed titles and their APP_VER; addcont.db holds the add-ons. Between them
 * they decide every "installed", "Update" and "DLC" the page draws. Two stats, so this is safe to
 * answer on the health poll, and the companion only has to notice the string changing to know it
 * should re-read the library - which is what makes the Updates section react to an install, an
 * update, a delete or an add-on WITHOUT the page being reloaded by hand.
 *
 * Deliberately not a hash of the file: a hash means reading it, and the point of this is that it
 * costs nothing. Size plus mtime moves for every write sqlite makes to either database. */
#define ADDCONT_DB_PATH "/system_data/priv/mms/addcont.db"
static void content_sig(char *out, size_t osz) {
    struct stat a, b;
    long long as = 0, am = 0, bs = 0, bm = 0;
    if (stat(APP_DB_PATH, &a) == 0) { as = (long long)a.st_size; am = (long long)a.st_mtime; }
    if (stat(ADDCONT_DB_PATH, &b) == 0) { bs = (long long)b.st_size; bm = (long long)b.st_mtime; }
    snprintf(out, osz, "%lld-%lld-%lld-%lld", as, am, bs, bm);
}

#define APPMETA_ROOT   "/user/appmeta"
/* The shop's own dashboard app. Same title id as the PS5 tile on purpose - one app, one identity,
   whichever console it is on - and it cannot collide with a game, which is CUSA or NPXS. */
#define PS4_TILE_TID   "PKGM00001"

/* Where an installed PS4 game's data actually lands. app.pkg WITH BYTES here is the only honest
   proof a title is installed - /user/appmeta survives an uninstall and would lie (the PS5 side
   learned that the hard way: 53 phantom installs). */
static const char *APP_ROOTS[] = { "/user/app", "/mnt/usb0/user/app", "/mnt/usb1/user/app", NULL };

/* ------------------------------------------------------------------ logging */

static pthread_mutex_t g_log_lock = PTHREAD_MUTEX_INITIALIZER;

static void ilog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void ilog(const char *fmt, ...) {
    char line[900];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(line, sizeof(line), fmt, ap);
    va_end(ap);
    klog_printf("[PMS] %s\n", line);
    pthread_mutex_lock(&g_log_lock);
    int fd = open(INSTALL_LOG, O_WRONLY | O_CREAT | O_APPEND, 0777);
    if (fd >= 0) {
        struct timespec ts;
        clock_gettime(CLOCK_REALTIME, &ts);
        char out[1000];
        int n = snprintf(out, sizeof(out), "%lld %s\n", (long long)ts.tv_sec, line);
        if (n > 0) { ssize_t w = write(fd, out, (size_t)n); (void)w; }
        close(fd);
    }
    pthread_mutex_unlock(&g_log_lock);
}

/* ------------------------------------------------------------- notifications
   Same struct and same call as the PS5 build, and the same rule: the PLAIN form only. The icon
   form returns success and draws nothing, which is how a release once shipped completely silent. */

typedef struct notify_request {
    char useless1[45];
    char message[3075];
} notify_request_t;
_Static_assert(sizeof(notify_request_t) == 0xC30, "notification struct must stay 0xC30");

int sceKernelSendNotificationRequest(int, notify_request_t *, size_t, int);
int sceKernelLoadStartModule(const char *, size_t, const void *, unsigned, void *, int *);
int sceUserServiceInitialize(void *);
int sceUserServiceGetForegroundUser(int *);
int sceUserServiceGetInitialUser(int *);
int sceAppInstUtilInitialize(void);

/* THE CONSOLE'S OWN LOCAL INSTALLER, and every signature here is COPIED FROM THE TOOLCHAIN'S OWN
 * HEADER (include/orbis/AppInstUtil.h), not inferred. That distinction is not pedantry: inferring
 * sceAppInstUtilCancelInstall's shape once crashed a console in this project, and this code runs
 * inside a SHARED system daemon where a crash takes the shop and the daemon with it.
 *
 * WHY THIS LANE EXISTS. Every install this shop has ever started on a PS4 went through BGFT, and
 * BGFT asks PlayStation Network whether the title has a newer version BEFORE it downloads anything.
 * When it finds one it builds a task with a SECOND piece in it - the update - fetches our package,
 * cannot fetch the update, and throws the whole thing away. Measured on this console:
 *
 *   Bluey's Quest for the Gold Pen  package 737,869,824 B, BGFT asked for 1,348,665,344, died at 89%
 *   Castle Crashers Remastered      the same, at 2%, rc 0x80990004, klog "[PATCH MERGE] error"
 *   Riptide GP2                     installed first time - the Store has no newer version of it
 *
 * That is the whole difference between the games that work and the games that do not, and nothing we
 * pass to BGFT changes it. All 14 of these symbols are present on 13.52 (probed, /api/engine/symprobe),
 * and none of them involves BGFT - so none of them can patch-check.
 *
 * HOLD THE OUTCOME TO THE SAME PROOF AS EVERYTHING ELSE. The PS5 half of this project has a hard-won
 * warning about the same-named call: there, AppInstallPkg registers metadata and installs no game
 * data, answering ok while nothing arrives. Whether that is also true on a PS4 is NOT known, so this
 * lane believes app.pkg CHANGING on disk and nothing else - exactly as the BGFT lane does. */
typedef int  (*pfn_ai_tid_from_pkg_t)(const char *pkg, char *tid_out, int *is_app);
typedef int  (*pfn_ai_exists_t)(const char *tid, int *exists);
typedef int  (*pfn_ai_prep_overwrite_t)(const char *pkg);
typedef int  (*pfn_ai_install_pkg_t)(const char *pkg, void *reserved);
typedef int  (*pfn_ai_progress_info_t)(const char *cid, unsigned *state, unsigned *progress,
                                       unsigned *done_sz, unsigned *total_sz, unsigned *rest_sec);
typedef int  (*pfn_ai_is_installing_t)(const char *cid);

static void notify(const char *msg) {
    notify_request_t req;
    memset(&req, 0, sizeof(req));
    snprintf(req.message, sizeof(req.message), "%s", msg);
    sceKernelSendNotificationRequest(0, &req, sizeof(req), 0);
}

static void notifyf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void notifyf(const char *fmt, ...) {
    char m[1200];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(m, sizeof(m), fmt, ap);
    va_end(ap);
    notify(m);
}

/* --------------------------------------------------------------- http output */

/* Returns 0 when every byte went out, -1 when the far end stopped reading. Most callers are
   writing a short JSON answer and have nothing to do about a failure, so they ignore it; the
   package stream does care, because carrying on would mean pushing gigabytes into a socket nobody
   is holding. With SIGPIPE ignored (see main) that failure arrives as EPIPE instead of killing the
   process we are injected into. */
static int write_all(int fd, const char *buf, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t w = write(fd, buf + off, len - off);
        if (w <= 0) return -1;
        off += (size_t)w;
    }
    return 0;
}

static void send_status(int fd, const char *status, const char *ctype, const char *body) {
    char hdr[512];
    int n = snprintf(hdr, sizeof(hdr),
                     "HTTP/1.1 %s\r\nContent-Type: %s\r\nContent-Length: %zu\r\n"
                     "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n",
                     status, ctype, strlen(body));
    write_all(fd, hdr, (size_t)n);
    write_all(fd, body, strlen(body));
}

static void send_json(int fd, const char *body) {
    send_status(fd, "200 OK", "application/json", body);
}

static const char *ctype_for(const char *path) {
    const char *d = strrchr(path, '.');
    if (!d) return "application/octet-stream";
    if (!strcmp(d, ".html")) return "text/html; charset=utf-8";
    if (!strcmp(d, ".js"))   return "application/javascript";
    if (!strcmp(d, ".css"))  return "text/css";
    if (!strcmp(d, ".png"))  return "image/png";
    if (!strcmp(d, ".webp")) return "image/webp";
    if (!strcmp(d, ".jpg") || !strcmp(d, ".jpeg")) return "image/jpeg";
    if (!strcmp(d, ".svg"))  return "image/svg+xml";
    if (!strcmp(d, ".ico"))  return "image/x-icon";
    if (!strcmp(d, ".json")) return "application/json";
    return "application/octet-stream";
}

/* Artwork is immutable for a title and is the expensive thing to re-fetch; the app shell must
   always revalidate or loading a new build leaves the old UI cached. Same policy as the PS5. */

/* CONDITIONAL GET - the other half of the ETag this server has always sent.
 *
 * send_file() has emitted `ETag: "<size>-<mtime>"` on every static file since caching was added,
 * and the comment above it says an unchanged shell then "costs only a 304". It could not: nothing
 * read If-None-Match back, so a browser that asked "still the same?" was answered with the whole
 * file every time. On the app shell that is ~810 KB per reload, down one console's accept loop,
 * queued behind the install engine.
 *
 * Matching is a SUBSTRING SEARCH over the header line, not an equality test, because the value may
 * be a list and because a cache is allowed to hand back a weakened tag (`W/"..."`) for one it
 * revalidates - an equality test would miss both and silently never hit. `*` matches whatever the
 * server holds, which is what the spec says it means. */
/* Defined further down, next to the other request helpers; declared here because this is
   the first thing in the file that needs it. */
static const char *strcasestr_local(const char *hay, const char *needle);
static int etag_matches(const char *req, const char *etag) {
    if (!req || !etag || !etag[0]) return 0;
    const char *h = strcasestr_local(req, "if-none-match:");
    if (!h) return 0;
    h += 14;
    const char *end = strstr(h, "\r\n");
    if (!end) end = h + strlen(h);
    size_t span = (size_t)(end - h);
    if (memchr(h, '*', span)) return 1;
    size_t n = strlen(etag);
    if (n == 0 || n > span) return 0;
    for (size_t i = 0; i + n <= span; i++)
        if (!memcmp(h + i, etag, n)) return 1;
    return 0;
}

/* `req` is the raw request text, or NULL from a caller with no request in hand. NULL means "never
   a 304", which is what every caller did before this parameter existed. */
static int send_file_req(int fd, const char *path, const char *req) {
    int f = open(path, O_RDONLY);
    if (f < 0) return -1;
    struct stat st;
    if (fstat(f, &st) != 0 || !S_ISREG(st.st_mode)) { close(f); return -1; }
    const char *ct = ctype_for(path);
    int is_img = ct && (strstr(ct, "image/") || strstr(ct, "font"));
    const char *cache = is_img ? "public, max-age=604800, immutable"
                               : "no-cache, must-revalidate";
    char etag[64];
    snprintf(etag, sizeof(etag), "\"%llx-%llx\"",
             (unsigned long long)st.st_size, (unsigned long long)st.st_mtime);
    if (etag_matches(req, etag)) {
        close(f);
        char h3[320];
        int n3 = snprintf(h3, sizeof(h3),
            "HTTP/1.1 304 Not Modified\r\nETag: %s\r\nCache-Control: %s\r\n"
            "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n", etag, cache);
        write_all(fd, h3, (size_t)n3);
        return 0;
    }
    char hdr[640];
    int n = snprintf(hdr, sizeof(hdr),
                     "HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %lld\r\n"
                     "Cache-Control: %s\r\nETag: %s\r\n"
                     "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n",
                     ct, (long long)st.st_size, cache, etag);
    write_all(fd, hdr, (size_t)n);
    char small[BULK_BUF_SMALL];
    size_t bufsz = 0;
    char *buf = bulk_buf_get(small, &bufsz);
    for (;;) {
        ssize_t r = read(f, buf, bufsz);
        if (r <= 0) break;
        write_all(fd, buf, (size_t)r);
    }
    bulk_buf_free(buf, small);
    close(f);
    return 0;
}
static int send_file(int fd, const char *path) { return send_file_req(fd, path, NULL); }

/* --------------------------------------------------------------- json/string */

/* Keeps valid UTF-8 (so "Bloodborne™" survives) and drops only genuinely broken bytes. */
static void json_escape(const char *in, char *out, size_t outsz) {
    size_t j = 0;
    for (size_t i = 0; in[i] && j < outsz - 2; i++) {
        unsigned char c = (unsigned char)in[i];
        if (c == '"' || c == '\\') { out[j++] = '\\'; out[j++] = (char)c; }
        else if (c == '\n' || c == '\r' || c == '\t') out[j++] = ' ';
        else if (c < 0x20) continue;
        else if (c >= 0x80) {
            int need = (c >= 0xF0) ? 3 : (c >= 0xE0) ? 2 : (c >= 0xC2) ? 1 : -1;
            if (need < 0) continue;
            int ok = 1;
            for (int k = 1; k <= need; k++)
                if (((unsigned char)in[i + k] & 0xC0) != 0x80) { ok = 0; break; }
            if (!ok) continue;
            if (j + (size_t)need + 1 >= outsz - 2) break;
            out[j++] = (char)c;
            for (int k = 1; k <= need; k++) out[j++] = in[i + k];
            i += (size_t)need;
        } else out[j++] = (char)c;
    }
    out[j] = 0;
}

static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}

static void url_decode(const char *in, char *out, size_t outsz) {
    size_t j = 0;
    for (size_t i = 0; in[i] && j < outsz - 1; i++) {
        if (in[i] == '%' && hexval(in[i + 1]) >= 0 && hexval(in[i + 2]) >= 0) {
            out[j++] = (char)((hexval(in[i + 1]) << 4) | hexval(in[i + 2]));
            i += 2;
        } else if (in[i] == '+') out[j++] = ' ';
        else out[j++] = in[i];
    }
    out[j] = 0;
}

/* ?key=value out of a raw request path, URL-decoded. */
static int qparam(const char *raw, const char *key, char *out, size_t outsz) {
    const char *q = strchr(raw, '?');
    if (!q) return 0;
    size_t klen = strlen(key);
    for (const char *p = q + 1; p && *p;) {
        const char *amp = strchr(p, '&');
        if (!strncmp(p, key, klen) && p[klen] == '=') {
            const char *v = p + klen + 1;
            size_t vlen = amp ? (size_t)(amp - v) : strlen(v);
            char enc[1200];
            if (vlen >= sizeof(enc)) vlen = sizeof(enc) - 1;
            memcpy(enc, v, vlen);
            enc[vlen] = 0;
            url_decode(enc, out, outsz);
            return 1;
        }
        if (!amp) break;
        p = amp + 1;
    }
    return 0;
}

static const char *strcasestr_local(const char *hay, const char *needle) {
    size_t n = strlen(needle);
    for (; *hay; hay++) {
        size_t i = 0;
        while (i < n) {
            char a = hay[i], b = needle[i];
            if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
            if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
            if (a != b) break;
            i++;
        }
        if (i == n) return hay;
    }
    return NULL;
}

static long long now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

static char *slurp(const char *path, long *out_len) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return NULL;
    struct stat st;
    if (fstat(fd, &st) != 0 || st.st_size <= 0) { close(fd); return NULL; }
    char *buf = (char *)malloc((size_t)st.st_size + 1);
    if (!buf) { close(fd); return NULL; }
    long got = 0;
    while (got < st.st_size) {
        ssize_t r = read(fd, buf + got, (size_t)(st.st_size - got));
        if (r <= 0) break;
        got += r;
    }
    close(fd);
    buf[got] = 0;
    if (out_len) *out_len = got;
    return buf;
}

static void mkparents(const char *path) {
    /* 1024, matching every path buffer that reaches here. It was 512 against callers holding
       char[1024] and char[1100], so a long path did not fail - snprintf truncated it and this
       silently created a DIFFERENT, shorter directory tree than the one asked for. */
    char tmp[1100];
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (char *p = tmp + 1; *p; p++)
        if (*p == '/') { *p = 0; mkdir(tmp, 0777); *p = '/'; }
}

static const char *lan_ip_str(void) {
    static char ip[24] = "127.0.0.1";
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s < 0) return ip;
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = inet_addr("8.8.8.8");
    a.sin_port = htons(53);
    if (connect(s, (struct sockaddr *)&a, sizeof(a)) == 0) {
        struct sockaddr_in me;
        socklen_t l = sizeof(me);
        if (getsockname(s, (struct sockaddr *)&me, &l) == 0)
            snprintf(ip, sizeof(ip), "%s", inet_ntoa(me.sin_addr));
    }
    close(s);
    return ip;
}

/* IS ANYTHING LISTENING ON ONE OF THIS CONSOLE'S OWN PORTS? Asked over loopback, because that is
   the only address this process can ask about without going out onto the network at all, and
   because a closed port there refuses at once rather than making the caller sit out a timeout.
   The two timeouts are set anyway: this runs on a connection thread, and a probe that hung would
   hold that thread for as long as it hung. Nothing is sent and nothing is read - the handshake IS
   the whole question, and it is the only thing we can honestly say about software that is not
   ours. */
static int port_listening(int port) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return 0;
    struct timeval tv;
    tv.tv_sec = 1; tv.tv_usec = 0;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)port);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    int up = (connect(s, (struct sockaddr *)&a, sizeof(a)) == 0);
    close(s);
    return up;
}

/* WHICH FTP THIS CONSOLE IS RUNNING, or 0 when it is running none.
 *
 * /api/health, /api/engine/state and /api/sources each stated "ftp_online":true and port 2121
 * whatever was there, so the Files row in Settings sat green and printed an address to type on a
 * PS4 where no FTP had ever been loaded. FTP is not ours: it is GoldHEN's, and it is up only if the
 * owner loaded it. /api/helpers was the one route that asked; now they all ask the same function,
 * which is also the only way anyone reading this can tell an answer from an assertion.
 *
 * 2121 first - GoldHEN's own, which onconsole/README.md documents as what this console runs - then
 * 1337, where the other PS4 FTP payloads listen. The PC companion already tries that pair in that
 * order (FTP_FALLBACKS in companion/server.py), so this asks rather than deciding for the user.
 *
 * MEMOISED FOR A MOMENT, because the page asks three of these routes the one question inside a
 * single refresh. The window is far shorter than the 6 s poll, so an FTP the owner has just loaded
 * still lights up on the next tick. The probe runs OUTSIDE the lock: a connect is bounded by the
 * kernel's retries rather than by the timeouts set above, and holding the lock across it would
 * queue every other request thread behind whoever happened to be probing. A thread arriving while a
 * probe is in flight takes the previous answer, which is no worse than probing alone. */
#define FTP_PROBE_TTL_MS 2000
static pthread_mutex_t g_ftp_lock = PTHREAD_MUTEX_INITIALIZER;
static long long       g_ftp_at;
static int             g_ftp_port;
static int             g_ftp_busy;

static int ftp_live_port(void) {
    pthread_mutex_lock(&g_ftp_lock);
    int fresh = g_ftp_at && (now_ms() - g_ftp_at) <= FTP_PROBE_TTL_MS;
    if (fresh || g_ftp_busy) {
        int have = g_ftp_port;
        pthread_mutex_unlock(&g_ftp_lock);
        return have;
    }
    g_ftp_busy = 1;
    pthread_mutex_unlock(&g_ftp_lock);

    int p = port_listening(2121) ? 2121 : (port_listening(1337) ? 1337 : 0);

    pthread_mutex_lock(&g_ftp_lock);
    g_ftp_port = p;
    g_ftp_at   = now_ms();
    g_ftp_busy = 0;
    pthread_mutex_unlock(&g_ftp_lock);
    return p;
}

/* ------------------------------------------------------- the PS4's app.db
 *
 * DIFFERENT SCHEMA FROM THE PS5, same path. The PS5 keeps one tbl_contentinfo; the PS4 keeps a
 * per-user tbl_appbrowse_<userid> (titleId, contentId, titleName, contentSize, category, ...)
 * plus a key/value tbl_appinfo (titleId, key, val) where the installed version lives under
 * APP_VER - which is exactly the field the companion already prefers for CUSA titles.
 *
 * category 'gd' is a real installed game; 'gdi' is a system stub with size 0 (Destiny, Spotify,
 * PS Now and friends ship as those and must never be listed as installed games).
 */

#define MAX_TITLES 512

typedef struct {
    char tid[16];
    char cid[64];
    char name[160];
    char ver[16];
    long long size;
    char category[8];
} ps4_title_t;

typedef struct {
    ps4_title_t *rows;
    int n, max;
    int i_tid, i_cid, i_name, i_size, i_cat;
} browse_ctx_t;

static void browse_cb(void *ctx, int ncol, char **v) {
    browse_ctx_t *c = (browse_ctx_t *)ctx;
    if (c->n >= c->max) return;
    if (c->i_tid < 0 || c->i_tid >= ncol) return;
    const char *tid = v[c->i_tid];
    if (!tid || strlen(tid) < 9) return;
    /* games only: CUSA/NPXS-style ids that are real content */
    const char *cat = (c->i_cat >= 0 && c->i_cat < ncol) ? v[c->i_cat] : "";
    if (!cat || strncmp(cat, "gd", 2)) return;      /* gd = installed game, gdi = system stub */
    if (!strcmp(cat, "gdi")) return;
    ps4_title_t *t = &c->rows[c->n];
    memset(t, 0, sizeof(*t));
    snprintf(t->tid, sizeof(t->tid), "%.15s", tid);
    snprintf(t->category, sizeof(t->category), "%.7s", cat);
    if (c->i_cid >= 0 && c->i_cid < ncol && v[c->i_cid]) snprintf(t->cid, sizeof(t->cid), "%.63s", v[c->i_cid]);
    if (c->i_name >= 0 && c->i_name < ncol && v[c->i_name]) snprintf(t->name, sizeof(t->name), "%.159s", v[c->i_name]);
    if (c->i_size >= 0 && c->i_size < ncol && v[c->i_size]) t->size = atoll(v[c->i_size]);
    c->n++;
}

typedef struct {
    ps4_title_t *rows;
    int n;
    int i_tid, i_key, i_val;
} appinfo_ctx_t;

static void appinfo_cb(void *ctx, int ncol, char **v) {
    appinfo_ctx_t *c = (appinfo_ctx_t *)ctx;
    if (c->i_tid < 0 || c->i_key < 0 || c->i_val < 0) return;
    if (c->i_tid >= ncol || c->i_key >= ncol || c->i_val >= ncol) return;
    const char *tid = v[c->i_tid], *key = v[c->i_key], *val = v[c->i_val];
    if (!tid || !key || !val) return;
    if (strcmp(key, "APP_VER")) return;             /* the installed version, PS4 spelling */
    for (int i = 0; i < c->n; i++)
        if (!strcmp(c->rows[i].tid, tid)) { snprintf(c->rows[i].ver, sizeof(c->rows[i].ver), "%.15s", val); return; }
}

/* Walk sqlite_master for a table whose name starts with `prefix`, then scan it. */
typedef struct { const char *prefix; char name[96]; char sql[4096]; uint32_t root; } find_ctx_t;

static void find_table_cb(void *ctx, int ncol, char **v) {
    find_ctx_t *f = (find_ctx_t *)ctx;
    if (ncol < 5 || f->root) return;
    if (!v[0] || strcmp(v[0], "table")) return;
    if (!v[1] || strncmp(v[1], f->prefix, strlen(f->prefix))) return;
    snprintf(f->name, sizeof(f->name), "%.95s", v[1]);
    snprintf(f->sql, sizeof(f->sql), "%.4095s", v[4] ? v[4] : "");
    f->root = (uint32_t)atoi(v[3] ? v[3] : "0");
}

/* Read the console's own list of installed titles. Returns how many were filled in. */
static int read_console_titles(ps4_title_t *out, int max) {
    long len = 0;
    char *data = slurp(APP_DB_PATH, &len);
    if (!data) return 0;
    sqdb_t db;
    if (sq_open(&db, (const uint8_t *)data, (size_t)len) != 0) { free(data); return 0; }

    find_ctx_t fb = { "tbl_appbrowse_", {0}, {0}, 0 };
    sq_walk(&db, 1, 5, find_table_cb, &fb, 0);
    if (!fb.root) { free(data); return 0; }

    browse_ctx_t bc;
    memset(&bc, 0, sizeof(bc));
    bc.rows = out; bc.max = max;
    bc.i_tid  = sq_col_index(fb.sql, "titleId");
    bc.i_cid  = sq_col_index(fb.sql, "contentId");
    bc.i_name = sq_col_index(fb.sql, "titleName");
    bc.i_size = sq_col_index(fb.sql, "contentSize");
    bc.i_cat  = sq_col_index(fb.sql, "category");
    int ncol = bc.i_cat + 1;
    for (int i = 0; i < 6; i++) { /* widen to the largest index we asked for */ }
    if (bc.i_tid + 1 > ncol) ncol = bc.i_tid + 1;
    if (bc.i_cid + 1 > ncol) ncol = bc.i_cid + 1;
    if (bc.i_name + 1 > ncol) ncol = bc.i_name + 1;
    if (bc.i_size + 1 > ncol) ncol = bc.i_size + 1;
    if (ncol < 1) { free(data); return 0; }
    sq_walk(&db, fb.root, ncol, browse_cb, &bc, 0);

    /* second pass: the installed version out of the key/value table */
    find_ctx_t fa = { "tbl_appinfo", {0}, {0}, 0 };
    sq_walk(&db, 1, 5, find_table_cb, &fa, 0);
    if (fa.root) {
        appinfo_ctx_t ac;
        memset(&ac, 0, sizeof(ac));
        ac.rows = out; ac.n = bc.n;
        ac.i_tid = sq_col_index(fa.sql, "titleId");
        ac.i_key = sq_col_index(fa.sql, "key");
        ac.i_val = sq_col_index(fa.sql, "val");
        int an = ac.i_tid;
        if (ac.i_key > an) an = ac.i_key;
        if (ac.i_val > an) an = ac.i_val;
        if (an >= 0) sq_walk(&db, fa.root, an + 1, appinfo_cb, &ac, 0);
    }
    free(data);
    return bc.n;
}


/* app.pkg WITH BYTES is the only honest proof. Returns its size, or 0. */
static long long installed_app_pkg(const char *tid) {
    for (int i = 0; APP_ROOTS[i]; i++) {
        char p[600];
        struct stat st;
        snprintf(p, sizeof(p), "%s/%s/app.pkg", APP_ROOTS[i], tid);
        if (stat(p, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0) return (long long)st.st_size;
    }
    return 0;
}

/* SIZE AND MTIME, because "app.pkg is there" is NOT proof that an install finished.
   It is proof for a FIRST install - there was no file and now there is one. For an UPDATE or a
   reinstall the file is already on disk, at very nearly the same size, from the version being
   replaced. Read as proof it says "finished" one second in, while the transfer is at 8%, and this
   shop then cancelled its own live install and deleted the package it was serving. Measured on
   13.52: `tx stopped (524288/6619136)`, error 0x0, the task gone from the table, app.db carrying
   the new version number and app.pkg still holding the old bytes.
   The console writes /user/app/<tid>/app.pkg at the END, in AppInstallApp, so a CHANGE to that
   file is honest proof for both cases. Returns 1 if the file exists at all. */
static int app_pkg_facts(const char *tid, long long *size, long long *mtime) {
    if (size)  *size = 0;
    if (mtime) *mtime = 0;
    for (int i = 0; APP_ROOTS[i]; i++) {
        char p[600];
        struct stat st;
        snprintf(p, sizeof(p), "%s/%s/app.pkg", APP_ROOTS[i], tid);
        if (stat(p, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0) {
            if (size)  *size  = (long long)st.st_size;
            if (mtime) *mtime = (long long)st.st_mtime;
            return 1;
        }
    }
    return 0;
}

/* WHERE A TITLE PROVES ITSELF DEPENDS ON WHAT WAS INSTALLED, and getting this wrong hangs a job
 * for ever rather than failing it.
 *
 * Measured: an UPDATE for Subnautica was installed and the job sat at "installing" indefinitely,
 * which the PC reported as 99% with no end. The install had in fact been handed over correctly - but
 * the finished check only ever looked at /user/app/<TID>/app.pkg, and the console writes an update to
 * /user/patch/<TID>/patch.pkg. A base game's file was never going to appear, so the job could never
 * reach a verdict either way.
 *
 * Categories are the package's own word for what it is, out of its param.sfo: gd a game, gp a patch,
 * ac additional content. With no category known - a caller that did not say - a change in ANY of the
 * three is accepted, because the alternative is the hang this replaces. */
static int title_proof_facts(const char *tid, const char *cat,
                             long long *size, long long *mtime) {
    if (size)  *size = 0;
    if (mtime) *mtime = 0;
    if (!tid || !tid[0]) return 0;
    int want_app   = !cat || !cat[0] || !strcmp(cat, "gd");
    int want_patch = !cat || !cat[0] || !strcmp(cat, "gp");
    int want_ac    = !cat || !cat[0] || !strcmp(cat, "ac");

    struct stat st;
    char p[700];
    if (want_app && app_pkg_facts(tid, size, mtime)) return 1;
    if (want_patch) {
        for (int i = 0; APP_ROOTS[i]; i++) {
            /* /user/app -> /user/patch, the same drive, the console's own layout */
            const char *root = APP_ROOTS[i];
            const char *tail = strstr(root, "/app");
            if (!tail) continue;
            snprintf(p, sizeof(p), "%.*s/patch/%s/patch.pkg",
                     (int)(tail - root), root, tid);
            if (stat(p, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0) {
                if (size)  *size  = (long long)st.st_size;
                if (mtime) *mtime = (long long)st.st_mtime;
                return 1;
            }
        }
    }
    if (want_ac) {
        for (int i = 0; APP_ROOTS[i]; i++) {
            const char *root = APP_ROOTS[i];
            const char *tail = strstr(root, "/app");
            if (!tail) continue;
            snprintf(p, sizeof(p), "%.*s/addcont/%s", (int)(tail - root), root, tid);
            DIR *d = opendir(p);
            if (!d) continue;
            struct dirent *e;
            long long newest = 0, bytes = 0;
            while ((e = readdir(d))) {
                if (e->d_name[0] == '.') continue;
                char q[900];
                snprintf(q, sizeof(q), "%s/%s/ac.pkg", p, e->d_name);
                if (stat(q, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0) {
                    if ((long long)st.st_mtime > newest) {
                        newest = (long long)st.st_mtime;
                        bytes  = (long long)st.st_size;
                    }
                }
            }
            closedir(d);
            if (newest) {
                if (size)  *size  = bytes;
                if (mtime) *mtime = newest;
                return 1;
            }
        }
    }
    return 0;
}

static int icon_path_for(const char *tid, char *out, size_t outsz) {
    struct stat st;
    snprintf(out, outsz, "%s/%s/icon0.png", APPMETA_ROOT, tid);
    if (stat(out, &st) == 0 && S_ISREG(st.st_mode)) return 1;
    for (int i = 0; APP_ROOTS[i]; i++) {
        snprintf(out, outsz, "%s/%s/sce_sys/icon0.png", APP_ROOTS[i], tid);
        if (stat(out, &st) == 0 && S_ISREG(st.st_mode)) return 1;
    }
    out[0] = 0;
    return 0;
}

/* ------------------------------------------------------------ the PC peers
   Each companion announces itself; we keep them all and hand the list to the page so it can pick
   one. Same contract as the PS5 build's /api/companion and /api/register-pc. */

#define PC_MAX 8
typedef struct { char ip[24]; int port; char name[64]; long long last_ms; char ver[16]; } pcpeer_t;
static pcpeer_t g_pcs[PC_MAX];
static pthread_mutex_t g_pcs_lock = PTHREAD_MUTEX_INITIALIZER;

static void pc_register(const char *ip, int port, const char *name, const char *ver) {
    if (!ip || !*ip || port <= 0) return;
    pthread_mutex_lock(&g_pcs_lock);
    int slot = -1, oldest = 0;
    for (int i = 0; i < PC_MAX; i++) {
        if (!g_pcs[i].ip[0]) { if (slot < 0) slot = i; continue; }
        if (!strcmp(g_pcs[i].ip, ip)) { slot = i; break; }
        if (g_pcs[i].last_ms < g_pcs[oldest].last_ms) oldest = i;
    }
    if (slot < 0) slot = oldest;
    snprintf(g_pcs[slot].ip, sizeof(g_pcs[slot].ip), "%.23s", ip);
    g_pcs[slot].port = port;
    if (name && *name) snprintf(g_pcs[slot].name, sizeof(g_pcs[slot].name), "%.63s", name);
    if (ver && *ver)   snprintf(g_pcs[slot].ver, sizeof(g_pcs[slot].ver), "%.15s", ver);
    g_pcs[slot].last_ms = now_ms();
    pthread_mutex_unlock(&g_pcs_lock);
}

/* HOW LONG A PC STAYS ON THE LIST AFTER IT STOPS ANNOUNCING ITSELF.
 *
 * A companion re-announces every 8 seconds while this console is up, so silence is real evidence
 * that it has gone. Nothing aged these entries out, on either console, and it cost the owner a
 * real symptom: a companion started on a spare port (a test run) announced itself, exited, and the
 * console went on offering http://<pc>:8791 to the page for hours. The page only walks down to a
 * dead entry when the live PC is briefly away - during a rebuild, say - and then the console's
 * browser is seen trying to reach a port nothing is listening on.
 *
 * Ten minutes, not ten seconds. A PC that is restarting - which is exactly what happens when the
 * owner installs a new build of it - must not be forgotten while it comes back, and the sort below
 * already puts the live one first, so a stale entry costs nothing until it is the only one left.
 * The window is long enough to survive a restart and short enough that a companion which is really
 * gone stops being offered. */
#define PC_STALE_MS (10 * 60 * 1000LL)

/* Which PC should the page try first: THE ONE HEARD FROM MOST RECENTLY. This build had no ordering
 * at all - it offered whichever array slot happened to be filled first, so "the PC to use" could be
 * a machine that had not said anything since the console booted.
 *
 * NOT ranked by version, deliberately, even though the PS5 build does rank by it. That rule exists
 * there for a specific measured reason: an old companion driving the console's UI brought back
 * behaviour that had been removed, including relaunching software this app no longer ships. None of
 * that history exists on the PS4 side, and copying the machinery would mean carrying a version
 * comparator this file does not otherwise have, for a case that has never happened here. If a PS4
 * ever does get bitten by an old companion, this is where the rule goes.
 *
 * Returns 1 when b should come before a. */
static int pc_better(const pcpeer_t *a, const pcpeer_t *b) {
    if (!b->ip[0]) return 0;
    if (!a->ip[0]) return 1;
    return b->last_ms > a->last_ms;
}

/* ---------------------------------------------------------- the install lane
 *
 * BGFT. We hand the console a URL on the companion and it downloads and installs by itself.
 * Every symbol is resolved by name at startup; a missing one means this route refuses in words
 * rather than crashing. See bgft.h for where the signatures come from.
 */

static pfn_bgft_init_t       bgft_init_fn;
static pfn_bgft_register_t   bgft_register_fn;
static pfn_bgft_start_t      bgft_start_fn;
static pfn_bgft_stop_t       bgft_stop_fn;
static pfn_bgft_unregister_t bgft_unreg_fn;
static pfn_bgft_progress_t   bgft_progress_fn;
static pfn_ai_tid_from_pkg_t   ai_tid_from_pkg_fn;
static pfn_ai_exists_t         ai_exists_fn;
static pfn_ai_prep_overwrite_t ai_prep_overwrite_fn;
static pfn_ai_install_pkg_t    ai_install_pkg_fn;
static pfn_ai_progress_info_t  ai_progress_info_fn;
static pfn_ai_is_installing_t  ai_is_installing_fn;
static int  g_bgft_ready = 0;
static void *g_bgft_heap = NULL;
#define BGFT_HEAP_SIZE (1 * 1024 * 1024)

/* the one job we are following, so /api/engine/job can answer the companion */
static pthread_mutex_t g_job_lock = PTHREAD_MUTEX_INITIALIZER;
static struct job_slot {
    int  active;
    OrbisBgftTaskId task;
    char tid[16];
    char cid[64];
    char name[160];
    char uri[1024];
    long long job_id;
    long long started_ms;
    long long done, total;
    /* LAST SEEN, as distinct from the start-of-job snapshot in base_size/base_mtime. The movement
       test needs "has it changed since the previous poll"; `replaced` further down needs "has it
       changed since the job began". One pair cannot answer both, and using base_* for movement made
       that test fire either never or always. */
    long long last_size, last_mtime;
    int last_seen;
    long long expect;      /* the package size the companion told us, for the finished check */
    long long seen_total;  /* the widest total BGFT ever claimed for this job - see job_refresh */
    long long base_size;   /* app.pkg for this title BEFORE this install started, and whether     */
    long long base_mtime;  /* there was one at all - the finished check compares against these,   */
    int  base_had;         /* because an update starts with the file already present.             */
    int  released;         /* its BGFT task has been handed back - do it once, not per poll */
    char cat[8];           /* the package's own category: gd a game, gp a patch, ac add-on. It
                              decides WHERE the finished install proves itself - see
                              title_proof_facts - and an update that is looked for in the base
                              game's place is a job that can never finish. */
    long long last_move_ms;/* when this job last did something: more bytes, or its file changed.
                              A job that stops moving has to end in a sentence, not in silence. */
    int  direct;           /* installed with the console's own installer, so it has no BGFT task
                              and its progress comes from AppInstUtil - see job_refresh */
    char state[24];        /* idle | downloading | installed | error */
    char msg[256];
    unsigned rc;
    int  held;             /* "+ Queue" put it here and it has not been started yet. The PS5 build
                              has had this since the day the queue existed; this one ignored
                              mode:"queued" entirely and installed on the spot, so the button that
                              exists to POSTPONE an install started one. */
    long long want_size;   /* WHAT BGFT WILL ASK FOR WHEN THIS IS RELEASED, kept beside the url.
                              A task needs the CONTENT id and the REAL size: a title id alone is
                              refused with 0x80990008 and a size of 0 is refused outright. The
                              on-console install route passed neither - it derived an id from the
                              package's FILE NAME, which yields a title id for any file named
                              "<Game>-CUSA#####.pkg", so every start answered "the console refused
                              this package". Measured: rc=0x80990008 id=CUSA02365 size=0. */
    char want_cid[64];
    char want_type[8];
} g_job;

/* ---------------------------------------------------------------- claiming the slot
 *
 * THE BUSY CHECK WAS CHECK-THEN-ACT, AND THE GAP WAS A WHOLE BGFT REGISTRATION WIDE.
 *
 * Every route that starts a transfer read `active` under the lock, RELEASED the lock, called
 * bgft_install_url() - which registers a task with the console and starts it - and only then took
 * the lock again to fill the slot in. Two requests arriving inside that gap both read "not busy",
 * both registered a task, and the second overwrote the slot: the first task went on downloading
 * with nothing following it, while progress, the finished check and the cancel button all described
 * the second package. That is the exact failure the busy check was added to prevent - it narrowed
 * the window instead of closing it. Five routes had the shape: install_local_pkg,
 * /api/engine/install-spawn, /api/queue/start, a row's /retry, and POST /api/install. (The
 * mode:"queued" branch never did: it tests and fills under one lock hold, which is the pattern
 * this brings to the rest.)
 *
 * The test and the claim happen under ONE lock hold now, and the registration runs against a slot
 * that is already marked taken. Between the claim and the commit the slot is active while
 * task is still BGFT_INVALID_TASK_ID, and g_job_claim holds that claim's token.
 *
 * WHY NOTHING ASKS BGFT ABOUT THAT SLOT. What the service does when handed a task id it never
 * issued is not written down in bgft.h, in any header this builds against, or anywhere in this
 * repo - and this payload runs as root inside a shared system daemon, where finding out by trying
 * is how you take the console down with you. The file already encodes the answer as a convention:
 * bgft_release() returns early on BGFT_INVALID_TASK_ID rather than passing it on. Everything that
 * could otherwise reach the service during a claim now follows that same convention - job_refresh()
 * skips its poll, the stranded-task sweeper does not run, and the two cancel routes guard the stop
 * call they were making unguarded. No invalid id is handed to BGFT, so what it would do with one
 * stays unknown and stays irrelevant.
 */
static struct job_slot g_job_before;      /* the row as it was, for an abort to put back */
static long long       g_job_claim;       /* which claim is outstanding, or 0 for none */
static long long       g_job_claim_seq;   /* hands out claim tokens; never reused */
static long long       g_job_claim_ms;    /* when, so a leaked claim cannot wedge the slot */
/* WHY A TOKEN AND NOT A FLAG. The first version of this committed against a boolean - "is a claim
   outstanding" - and that is not the question. A cancel arriving mid-registration clears the claim,
   which is correct and is what lets the committer know to hand its task back; but it also leaves
   the slot free, so a SECOND install can claim it before the first returns from bgft_install_url.
   The first would then find the boolean set again, believe the claim was still its own, and write
   its task id over the second one's row: two live tasks, one slot, and the loser downloading with
   nothing following it. That is the bug this change removes, reintroduced by the fix for it.
   A token makes the test identity instead of existence - you can only commit the claim you
   opened. */

/* Long enough that no honest registration reaches it - bgft_install_url is a handful of service
   calls and has never been seen to take seconds - and short enough that if a claim ever did leak,
   the console frees itself instead of needing the payload reloaded. That lock-out, arrived at by a
   different route, is a bug this shop has already shipped once. */
#define JOB_CLAIM_TIMEOUT_MS (3 * 60 * 1000LL)

typedef enum {
    JOB_CLAIM_FRESH,   /* a new install: the slot must be free, or holding a finished job */
    JOB_CLAIM_DIRECT,  /* the same, for the console's own installer - no task, ever */
    JOB_CLAIM_HELD,    /* /api/queue/start: a queued row waiting to be released */
    JOB_CLAIM_RETRY    /* a row that failed and still knows the url it failed on */
} job_claim_kind;

/* What a caller needs out of the row it just claimed. A FRESH caller brings its own and passes
   NULL; HELD and RETRY have nothing else to install from, which is the whole point of a queue on a
   console with no PC in the room. */
typedef struct {
    char uri[1024];
    char cid[64];
    char type[8];
    char name[160];
    long long size;
} job_claim_out;

/* The claim's TOKEN when the slot is taken, or 0 - in which case nothing was touched and the
   caller must refuse. The caller MUST reach job_claim_abort(tok), or a commit that tests the token,
   on every path out. */
static long long job_claim(job_claim_kind kind, job_claim_out *out) {
    pthread_mutex_lock(&g_job_lock);
    int ok = 0;
    switch (kind) {
    case JOB_CLAIM_FRESH:
    case JOB_CLAIM_DIRECT:
        /* The same test the routes each used to spell out for themselves, in one place. */
        ok = !(g_job.active && strcmp(g_job.state, "installed") && strcmp(g_job.state, "error"));
        break;
    case JOB_CLAIM_HELD:
        ok = g_job.held && !g_job.active && g_job.uri[0];
        break;
    case JOB_CLAIM_RETRY:
        ok = g_job.uri[0] && (!g_job.active || !strcmp(g_job.state, "error"));
        break;
    }
    if (!ok || g_job_claim) { pthread_mutex_unlock(&g_job_lock); return 0; }

    g_job_before = g_job;
    if (out) {
        snprintf(out->uri,  sizeof(out->uri),  "%s", g_job.uri);
        snprintf(out->cid,  sizeof(out->cid),  "%s", g_job.want_cid);
        snprintf(out->type, sizeof(out->type), "%s", g_job.want_type);
        snprintf(out->name, sizeof(out->name), "%s", g_job.name);
        out->size = g_job.want_size;
    }
    /* FRESH and DIRECT start from nothing; HELD and RETRY keep the url they are about to reuse. */
    if (kind == JOB_CLAIM_FRESH || kind == JOB_CLAIM_DIRECT) memset(&g_job, 0, sizeof(g_job));
    /* A RETRY IS A NEW ATTEMPT, NOT A CONTINUATION. done, total, rc and seen_total survived the claim
       because only FRESH and DIRECT memset the slot - so a retry published the FAILED attempt's error
       code and percentage until the first poll overwrote them, and if it failed again before that the
       owner read the previous failure's code as if it were this one's. The url, name and want_* stay:
       that is what a retry retries. */
    if (kind == JOB_CLAIM_RETRY) {
        g_job.done = g_job.total = g_job.seen_total = 0;
        g_job.rc = 0;
        g_job.base_had = 0; g_job.base_size = 0; g_job.base_mtime = 0;
        g_job.last_seen = 0; g_job.last_size = 0; g_job.last_mtime = 0;
    }
    g_job.active   = 1;
    g_job.held     = 0;
    g_job.released = 0;
    g_job.direct   = (kind == JOB_CLAIM_DIRECT);
    g_job.task     = BGFT_INVALID_TASK_ID;
    snprintf(g_job.state, sizeof(g_job.state), "downloading");
    snprintf(g_job.msg, sizeof(g_job.msg), "Handing it to the console");
    g_job_claim    = ++g_job_claim_seq;
    g_job_claim_ms = now_ms();
    long long mine = g_job_claim;
    pthread_mutex_unlock(&g_job_lock);
    return mine;
}

/* Put the row back exactly as it was. A held row is held again and a failed row can still be
   retried - which is why this restores a copy rather than clearing the slot. */
static void job_claim_abort(long long tok) {
    pthread_mutex_lock(&g_job_lock);
    /* Only OUR claim. If a cancel already took it, the row belongs to whatever came after and
       restoring the copy we saved would undo their work as well as ours. */
    if (tok && g_job_claim == tok) {
        g_job = g_job_before;
        g_job_claim = 0;
    }
    pthread_mutex_unlock(&g_job_lock);
}

/* Call with the lock HELD, from inside a commit: is the slot still the one we claimed? */
static int job_claim_is_mine_locked(long long tok) {
    return tok && g_job_claim == tok;
}

/* ============================ MEMORY: CAN WE REACH A PROCESS? ============================
 *
 * Read the file header first - this is the one thing that stands between this console and the
 * cheat engine, and everything below is measurement, not capability.
 *
 * THE STUB IS COPIED, NOT WRITTEN. This is ps4-payload-sdk's crt/syscall.h __syscall, verbatim
 * (GPLv3, the same SDK whose crt this payload already links). It is here rather than included
 * because crt/ is internal to the SDK build and is not on our include path. Do not "tidy" it: the
 * clobber list and the carry-flag output are the error convention this kernel uses, and a syscall
 * stub written from memory is exactly the class of guess this project has a scar from.
 */
static inline long pms_syscall(long n, ...) {
    long a1 = 0, a2 = 0, a3 = 0, a4 = 0, a5 = 0, a6 = 0;
    __builtin_va_list ap;
    unsigned long ret;
    char err;

    __builtin_va_start(ap, n);
    a1 = __builtin_va_arg(ap, long);
    a2 = __builtin_va_arg(ap, long);
    a3 = __builtin_va_arg(ap, long);
    a4 = __builtin_va_arg(ap, long);
    a5 = __builtin_va_arg(ap, long);
    a6 = __builtin_va_arg(ap, long);
    __builtin_va_end(ap);

    register long r10 asm("r10") = a4;
    register long r8  asm("r8")  = a5;
    register long r9  asm("r9")  = a6;

    asm("syscall"
        : "=a"(ret), "=@ccc"(err), "+r"(r10), "+r"(r8), "+r"(r9)
        : "a"(n), "D"(a1), "S"(a2), "d"(a3)
        : "rcx", "r11", "memory");

    return err ? -ret : ret;
}

/* All four numbers below are read from the SDK, not chosen. crt/syscall.h for 573; crt/mdbg.c for
   the two memory operations and for the fact that cmd.type is 1. */
#define PMS_SYS_MDBG_CALL 573
#define PMS_MDBG_READ     0x12
#define PMS_MDBG_WRITE    0x13

typedef struct { unsigned long type, cmd; } pms_mdbg_cmd_t;
typedef struct { int pid; unsigned long src, dst, len; } pms_mdbg_args_t;
typedef struct { int status; unsigned long len; } pms_mdbg_res_t;

/* ONE memory operation, with NO credential juggling.
 * The SDK's mdbg_memop() elevates to SCE_AUTHID_COREDUMP through kexec before calling, and that is
 * the only reason it cannot work here. This issues the same syscall with the same three structures
 * and nothing else, so what comes back is the kernel's own answer about the credentials GoldHEN
 * left us with. `res` is filled for the caller: a probe wants the status and length, not a verdict.
 */
static long pms_mdbg_op(int op, pms_mdbg_args_t *a, pms_mdbg_res_t *res) {
    pms_mdbg_cmd_t cmd;
    cmd.type = 1;
    cmd.cmd  = (unsigned long)op;
    res->status = 0;
    res->len    = 0;
    return pms_syscall(PMS_SYS_MDBG_CALL, (long)&cmd, (long)a, (long)res);
}

/* ============================================================================================
 * THE JAILBREAK'S OWN KERNEL GATEWAY - read/write another process, the PS4 way.
 *
 * The PS5 reaches a running game's memory through kernel access its jailbreak hands our payload.
 * On the PS4 the two direct doors to that (mdbg 573, ptrace 26) are shut for us - measured. The
 * jailbreak on this console installs a gateway of its own instead: one indirect syscall, number
 * 500, dispatched by a command word, and its own memory tooling goes through it. We use it the way
 * we use the PS5's kernel access - it is what the jailbreak provides, not a second app running
 * inside the game (that approach hung games and is gone).
 *
 * gh_orbis_syscall IS COPIED VERBATIM from the SDK's own source/Syscall.c - the INDIRECT form:
 * rax = 0, the real number (500) travels in the first argument. Our existing pms_syscall uses the
 * DIRECT form (number in rax) and that is a different calling convention which the binaries on
 * this console are NOT observed to use for this gateway (they issue mov edi,500 - number in edi),
 * so it must not be reused here. The structs and command numbers are the SDK header's, verbatim.
 * ============================================================================================ */
__asm__(
    ".att_syntax prefix\n"
    ".globl gh_orbis_syscall\n"
    "gh_orbis_syscall:\n"
    "  movq $0, %rax\n"
    "  movq %rcx, %r10\n"
    "  syscall\n"
    "  jb 1f\n"
    "  retq\n"
    "1:\n"
    "  pushq %rax\n"
    "  callq __error\n"
    "  popq %rcx\n"
    "  movl %ecx, 0(%rax)\n"
    "  movq $0xFFFFFFFFFFFFFFFF, %rax\n"
    "  movq $0xFFFFFFFFFFFFFFFF, %rdx\n"
    "  retq\n"
);
extern long gh_orbis_syscall(long num, ...);

/* SDK header include/GoldHEN.h, verbatim. The command numbers and the two structs. */
#define GH_SDK_CMD_VERSION       0
#define GH_SDK_CMD_PROCESS_INFO  4
#define GH_SDK_CMD_PROCESS_RW    5
#define GH_SDK_VERSION_EXPECTED  0x00000100
#define GH_SDK_SYSCALL           500

typedef struct {
    int pid;
    char name[40];
    char path[64];
    char titleid[16];
    char contentid[64];
    char version[6];
    unsigned long base_address;
} __attribute__((packed)) gh_proc_info_t;

typedef struct {
    unsigned long address;
    void *data;
    unsigned long length;
    unsigned long write_flags;
} __attribute__((packed)) gh_proc_rw_t;

/* jailbreak_backup, verbatim from the SDK header include/GoldHEN.h. Opaque to us: cmd 2 fills it,
   cmd 3 restores from it, and we never modify a field between the two. */
#define GH_SDK_CMD_JAILBREAK    2
#define GH_SDK_CMD_UNJAILBREAK  3
typedef struct {
    unsigned int  cr_uid;
    unsigned int  cr_ruid;
    unsigned int  cr_rgid;
    unsigned int  cr_groups;
    unsigned long cr_paid;
    unsigned long cr_caps[2];
    void *cr_prison;
    void *fd_cdir;
    void *fd_jdir;
    void *fd_rdir;
} gh_jailbreak_backup_t;

/* Handles from dlopen, kept so we can search them by name.
   RTLD_DEFAULT ALONE IS NOT ENOUGH: measured on 13.52, every BGFT symbol came back NULL from the
   default scope and resolved fine from the handle of the library we opened. The first build of
   this file searched only RTLD_DEFAULT and refused every install with "BGFT is incomplete on this
   firmware", which was our own lookup being wrong, not the console lacking the calls. */
#define DL_MAX 8
static void *g_dl[DL_MAX];
static int   g_dl_n;

static void *dlsym_any(const char *sym) {
    void *p = dlsym(RTLD_DEFAULT, sym);
    for (int i = 0; !p && i < g_dl_n; i++) p = dlsym(g_dl[i], sym);
    return p;
}

/* ---- WHICH GAME IS RUNNING -------------------------------------------------------------------
 * Every number and every name here was read off this machine, and the one thing the two PS4
 * toolchains disagreed about was settled by asking the console (see the file-level note on
 * /api/engine/proclist).
 *
 *   sceSystemServiceGetAppIdOfBigApp()  - the PS4 name. The PS5 half of this repo calls
 *     sceSystemServiceGetAppIdOfRunningBigApp, which does not exist on a PS4; copying the PS5's
 *     name over is precisely the mistake this file's old comment warned about.
 *   sceKernelGetAppInfo(pid, info)      - libkernel, and it needs libkernel to have been dlopen'd
 *     (it is, now - it resolved from nowhere until it was added to bgft_bootstrap's list).
 *   title id at info+16, 10 bytes       - MEASURED on 13.52. Offset 12 was empty on all 63.
 */
#define PMS_APPINFO_TID_OFF 16
#define PMS_APPINFO_TID_LEN 10

static pthread_mutex_t g_rt_lock = PTHREAD_MUTEX_INITIALIZER;
static char      g_rt_id[16];
static long long g_rt_at;
#define RT_TTL_MS 2000

/* The title id of the foreground game, or "" when no game is running.
   Never fails loudly: an empty string is the honest answer to "I could not tell", and that is what
   the page already treats as "no game to highlight". */
/* THE ONLY THING /api/health CALLS. A copy out of the cache under the mutex - no syscalls, no
   process walk, no way to block. Everything expensive happens in rt_thread() below.

   This used to do the work inline, and when a game crashed mid-load the call never returned:
   /api/health connected and hung for ever while GET / served the whole 1.2 MB page in 34 ms, so
   the UI sat on "please wait" with nothing to fail on. Every poll from every device then started
   another copy of the same walk. A stale title id is a harmless answer; a health route that does
   not answer takes the entire app down on every device at once. */
static void running_title_id(char *out, size_t n) {
    if (!n) return;
    pthread_mutex_lock(&g_rt_lock);
    snprintf(out, n, "%s", g_rt_id);
    pthread_mutex_unlock(&g_rt_lock);
}

/* The expensive half, on its own thread and nowhere near a request.
   If THIS blocks on a dying process the only consequence is that the cached answer stops being
   refreshed - the shop keeps serving, which is the entire reason for the split. */
static void rt_refresh_once(void) {
    char found[16];
    found[0] = 0;

    int (*bigapp)(void) = (int (*)(void))dlsym_any("sceSystemServiceGetAppIdOfBigApp");
    int (*getappinfo)(pid_t, void *) = (int (*)(pid_t, void *))dlsym_any("sceKernelGetAppInfo");
    int app_id = (bigapp && getappinfo) ? bigapp() : -1;

    /* A game, and only a game. With nothing running this call answers with a non-positive id and
       there is nothing to look up - the system processes all carry NPXS ids and must never be
       reported as "the running game". */
    if (app_id > 0) {
        int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PROC, 0 };
        size_t need = 0;
        if (!sysctl(mib, 4, NULL, &need, NULL, 0) && need) {
            need += need / 8 + 8192;
            char *buf = (char *)malloc(need);
            if (buf) {
                if (!sysctl(mib, 4, buf, &need, NULL, 0)) {
                    for (char *p = buf; p < buf + need; ) {
                        struct kinfo_proc *ki = (struct kinfo_proc *)p;
                        if (ki->ki_structsize <= 0 || p + ki->ki_structsize > buf + need) break;
                        p += ki->ki_structsize;

                        unsigned char info[0x100];
                        memset(info, 0, sizeof(info));
                        if (getappinfo(ki->ki_pid, info)) continue;
                        if (*(unsigned int *)info != (unsigned int)app_id) continue;

                        char tid[PMS_APPINFO_TID_LEN + 1];
                        memcpy(tid, info + PMS_APPINFO_TID_OFF, PMS_APPINFO_TID_LEN);
                        tid[PMS_APPINFO_TID_LEN] = 0;
                        for (int i = 0; tid[i]; i++)
                            if (tid[i] < 32 || tid[i] > 126) { tid[i] = 0; break; }
                        /* A GAME, AND ONLY A GAME - which is what the comment at the top of
                           this function has always said and what the PS5 does at the same point.
                           This used to accept NPXS as well, and NPXS is precisely what it must
                           not accept: those are the system processes, and the shell, the store and
                           THE BROWSER are all of them. So the moment the owner opened this page on
                           the console itself, the "running game" became a system app - matching no
                           card in the library, floating nothing to the top, and naming a title
                           with no cheats to /api/cheat/running. */
                        if (!strncmp(tid, "CUSA", 4) || !strncmp(tid, "PPSA", 4)) {
                            snprintf(found, sizeof(found), "%s", tid);
                            break;
                        }
                    }
                }
                free(buf);
            }
        }
    }

    pthread_mutex_lock(&g_rt_lock);
    snprintf(g_rt_id, sizeof(g_rt_id), "%s", found);
    g_rt_at = now_ms();
    pthread_mutex_unlock(&g_rt_lock);
}

/* Defined further down, beside the channel it reads. Declared here because the watcher below is the
   cheapest place to notice that an agent has appeared. */
static int agent_alive_fresh(void);

/* Defined beside agent_info, far below. Declared here because BOTH the watcher and agent_purge sit
   above it and both end a session whose cached status must not outlive it. */
static void agent_info_forget(void);

/* RAISED WHEN WHAT IS INSTALLED CHANGES, lowered by the watcher once it has re-decided who should be
   armed. A flag rather than a direct call because the place that knows an install finished holds
   g_job_lock, and re-deciding reads app.db and rewrites the owner's plugin list - neither belongs
   under a lock that a progress poll is waiting on.

   THE WAIT IS NOT WHAT MAKES THE DECISION FRESH - the watcher dropping the cache itself is. This
   comment used to claim the two-second wait let the cache be rebuilt first; it cannot, because the
   TTL is five seconds and the one fresh read the install branch's drop buys is taken by whoever
   asks next (console_lists_title four lines later, or any poll of the library). So the watcher
   drops it again immediately before reconciling, and the reconcile IS the read that goes to
   app.db. */
static volatile int g_autoarm_dirty;

/* Both live far below, beside the rest of autoarm - declared here because the watcher above them is
   the thread that acts on the flag. */
static int autoarm_enabled(void);
static int agent_autoarm_reconcile(void);

/* THE HANDOVER FLAG, declared here so the watcher can test it - its definition is ~2,600 lines below,
   which is exactly why the watcher never tested it and leaked on every reload. A tentative definition
   at file scope is legal C and the one with the initialiser still owns the storage. */
static volatile int g_quit;

static void *rt_thread(void *arg) {
    (void)arg;
    /* WHICH GAME WE HAVE ALREADY ANNOUNCED. Not a boolean: the owner can close one game and open
       another without the shop restarting, and each deserves its own message exactly once.
       CLEARED BY THE AGENT'S HEARTBEAT GOING STALE - never by the running title going empty, which is
       the bug this replaced. See the note beside the clear below. */
    static char announced_for[16];
    static int  no_agent_polls = 0;

    /* NOT `for (;;)`, AND THAT IS THE WHOLE POINT. GoldHEN injects every payload into one shared host
       process, so /api/quit cannot _exit() - it sets g_quit, stops the accept loop and closes the
       listening socket so the new instance can bind. Its comment claimed "our threads end"; this one
       did not, because it never tested the flag. Every reload therefore left another copy of this loop
       walking the entire process list every two seconds, for ever, inside ScePartyDaemon.
       The owner found it the hard way: the engine's own message arrived once per leaked watcher, so it
       went 2, then 3, in step with the number of payload pushes. I had blamed a flicker in the running
       title and been wrong - the count going UP after that "fix" was the proof. */
    while (!g_quit) {
        rt_refresh_once();

        /* SOMETHING WAS INSTALLED (or re-installed) SINCE WE LAST DECIDED. Re-deciding is cheap when
           the answer has not moved - agent_autoarm_apply compares the whole result with what is on
           disk and writes nothing if they match - so this costs a directory read and a compare. */
        /* DID THE CONSOLE'S CONTENT CHANGE, whoever changed it? content_sig is a stat of app.db and
           addcont.db - the same string /api/health reports - so this notices an install, an update,
           a DELETE from the console's own menu, or an add-on, for the price of two stats every two
           seconds. It matters because the install-completion branch is the only other thing that
           raises this flag, and it fires on install only: uninstalling a game used to leave the
           in-game helper listed for a title that no longer exists until the next reboot.
           The FIRST observation only remembers - startup_extras has already reconciled by then, and
           a second pass for the same state would rewrite the owner's plugin list for nothing. */
        {
            static char last_sig[64];
            static long long last_act_ms = 0;
            static int sig_pending = 0;
            char sig_now[64];
            content_sig(sig_now, sizeof(sig_now));
            if (!last_sig[0]) {
                snprintf(last_sig, sizeof(last_sig), "%s", sig_now);
            } else if (strcmp(last_sig, sig_now)) {
                snprintf(last_sig, sizeof(last_sig), "%s", sig_now);
                sig_pending = 1;
            }
            /* NOT ON EVERY TICK. Measured in the console's own log: a single tile install moved the
               signature three times in four seconds, and each one re-read app.db and re-decided the
               owner's plugin list. Re-deciding is idempotent (gh_ini_write_whole writes nothing when
               the answer is unchanged), so this is waste rather than damage - but it is waste inside a
               system daemon whose heap we do not own. Ten seconds collapses an install's worth of
               writes into one pass, and `sig_pending` is why a change during the cooldown is not lost. */
            if (sig_pending && now_ms() - last_act_ms > 10000) {
                sig_pending = 0;
                last_act_ms = now_ms();
                titles_cache_drop();
                g_autoarm_dirty = 1;
                ilog("content: what is installed changed - re-reading and re-deciding the helper");
            }
        }

        if (g_autoarm_dirty) {
            g_autoarm_dirty = 0;
            if (autoarm_enabled()) {
                /* THE RECONCILE MUST BE THE READ THAT GOES TO app.db. The drop the install branch
                   made two seconds ago has already been spent by whoever asked next, and the cache
                   holds that answer for five seconds - so without this, the decision about which
                   titles carry the helper is made from a snapshot taken at the instant the install
                   finished, which is exactly when app.db may not carry the row yet. */
                titles_cache_drop();
                int armed = agent_autoarm_reconcile();
                if (armed >= 0)
                    ilog("autoarm: %d installed title(s) with cheats are armed", armed);
            }
        }

        /* OUR OWN MESSAGE, INSTEAD OF GOLDHEN'S GOLD ONE.
           Its loader draws "Loaded 1 plugin(s) 1. <name>" the instant the module is mapped, in its own
           styling - and "mapped" is not the useful moment. The agent sleeps six seconds before it
           touches anything, so for those six seconds that banner is telling the owner something is
           ready when nothing is. This fires when alive.bin says the agent is actually serving, which is
           the moment cheats will really work. Arming switches GoldHEN's banner off (see
           gh_set_load_notification) so this is the only message. One stat, no round trip - the watcher
           already runs every two seconds and this costs it nothing. */
        char cur[16] = {0};
        pthread_mutex_lock(&g_rt_lock);
        snprintf(cur, sizeof(cur), "%s", g_rt_id);
        pthread_mutex_unlock(&g_rt_lock);

        /* ONE MESSAGE PER GAME.
           A SCAR ABOUT DIAGNOSIS, not about this code. The owner saw this message twice and I blamed a
           flicker in the running title during a game load - having sampled the title and observed ZERO
           flicker, and asserted the cause anyway. The real cause was that every payload reload leaked
           this whole thread (see the while (!g_quit) above), so the message arrived once per leaked
           watcher. The count went 2, then 3, in step with the pushes - and my "fix" made it worse,
           which should have ended the theory on the spot.
           The keying on alive.bin is kept because it IS the better signal: the agent rewrites it every
           two seconds and it is stale only when there is no agent, which is exactly when another
           announcement is warranted - whereas the running title also moves for reasons that have
           nothing to do with our helper. Two consecutive misses are required so one slow beat cannot
           produce a repeat. But it was never the bug. */
        if (!agent_alive_fresh()) {
            /* No agent: whatever the cached status describes is a session that has ended. */
            agent_info_forget();
            if (++no_agent_polls >= 2) announced_for[0] = 0;
        } else {
            no_agent_polls = 0;
            if (cur[0] && strcmp(announced_for, cur) != 0) {
                notify("PKG MUTANT SHOP: the cheat engine is on for this game. "
                       "Switch cheats on and off from the app.");
                snprintf(announced_for, sizeof(announced_for), "%s", cur);
            }
        }

        usleep(RT_TTL_MS * 1000);
    }
    ilog("watcher: standing down for the new instance");
    return NULL;
}

/* ---- THIS CONSOLE'S DURABLE ID -----------------------------------------------------------------
 * Sixteen hex characters in a file beside the shop's own data, generated once and kept for ever.
 * It exists so a PC can recognise this console after its ADDRESS changes - which happens on any
 * network with DHCP, and which used to leave the app showing a healthy console as offline because
 * the only name it had for it was the address that moved.
 *
 * Random and local. Not a serial number, not a MAC, not an account. Delete the file and the console
 * gets a new one; a PC then treats it as a console it has never met, which is the honest reading.
 */
#define CONSOLE_ID_PATH SHOP_DATA_DIR "/console-id"

static char g_console_id[20];

static const char *console_id(void) {
    if (g_console_id[0]) return g_console_id;

    long n = 0;
    char *have = slurp(CONSOLE_ID_PATH, &n);
    if (have) {
        int k = 0;
        for (long i = 0; i < n && k < 16; i++) {
            char c = have[i];
            if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f')) g_console_id[k++] = c;
            else if (c == '\n' || c == '\r' || c == ' ' || c == '\t') continue;
            else { k = 0; break; }          /* not ours - regenerate rather than trust it */
        }
        free(have);
        if (k == 16) { g_console_id[16] = 0; return g_console_id; }
        g_console_id[0] = 0;
    }

    /* Enough for a name that only has to be unique across the consoles in one house: the clock,
       our pid, and two addresses that move with ASLR from run to run. */
    unsigned long long seed = (unsigned long long)now_ms();
    seed ^= ((unsigned long long)getpid() << 32);
    seed ^= (unsigned long long)(uintptr_t)&seed;
    seed ^= ((unsigned long long)(uintptr_t)g_console_id) << 13;
    static const char HEX[] = "0123456789abcdef";
    for (int i = 0; i < 16; i++) {
        seed = seed * 6364136223846793005ULL + 1442695040888963407ULL;
        g_console_id[i] = HEX[(seed >> 33) & 0xF];
    }
    g_console_id[16] = 0;

    mkdir(SHOP_DATA_DIR, 0777);
    int f = open(CONSOLE_ID_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (f >= 0) { ssize_t w = write(f, g_console_id, 16); (void)w; close(f); }
    return g_console_id;
}

/* ---- THE IN-GAME HELPER'S OWN SWITCH ---------------------------------------------------------
 * GoldHEN's loader reads /data/GoldHEN/plugins/plugins.ini when a game starts: a line naming a
 * .prx loads it, the same line behind a ';' does not. That is the whole mechanism, and it is what
 * these two functions edit - one line, ours, leaving every other line exactly as it was.
 */
/* /data/GoldHEN/plugins.ini - NOT inside the plugins/ folder, which is where this file put
   it at first. The plugin loader names the path in its own log when it cannot read it:
     [GoldHEN] <Plugins Loader> Config parser failed to parse config: /data/GoldHEN/plugins.ini
   The folder holds the .prx files; the list of which ones to load sits beside it. Measured
   on a real game launch - our plugin was installed, listed, enabled and never loaded, and
   the only sign was that one line. */
#define GH_PLUGINS_INI  "/data/GoldHEN/plugins.ini"
#define PMS_AGENT_PRX   "/data/GoldHEN/plugins/pms-agent.prx"
/* Written once, the first time this console lists the plugin for itself. See agent_deploy(). */
#define AGENT_SETUP_MARK SHOP_DATA_DIR "/agent-listed-once"

/* IS THIS LINE A LISTING OF OUR PLUGIN - not "does this line mention it". It was strstr() of the
   bare name, which is true of any comment that talks about the file, including the prose header
   agent_set_enabled itself writes ("PKG MUTANT SHOP in-game helper ... pms-agent.prx" never appears
   there today, but the next edit to that text is one word away from arming the reporting).

   What counts: optional leading whitespace, then optionally our own "off" spelling of ';' plus
   whitespace, then the FIRST token - ending at space, tab, CR, or a ';' that begins a trailing note,
   none of which can occur inside a path. That token is a listing when it is exactly the bare name
   or ends in "/pms-agent.prx".

   Accepts: the absolute path; "; " + the path; a CRLF line (the callers' line buffers keep the
   trailing CR, and agent_enable_for_title already special-cases it); a trailing inline comment; the
   file under any other directory; the bare name. Rejects prose that merely names the file. */
static int agent_line_is_ours(const char *line) {
    const char *p = line;
    while (*p == ' ' || *p == '\t') p++;
    if (*p == ';') { p++; while (*p == ' ' || *p == '\t') p++; }
    const char *start = p;
    while (*p && *p != ' ' && *p != '\t' && *p != '\r' && *p != ';') p++;
    size_t tl = (size_t)(p - start);
    static const char NAME[] = "pms-agent.prx";
    const size_t nl = sizeof(NAME) - 1;
    if (tl < nl) return 0;
    if (memcmp(start + tl - nl, NAME, nl) != 0) return 0;
    return tl == nl || start[tl - nl - 1] == '/';
}

/* ONE WHOLE-FILE WRITER FOR THE OWNER'S PLUGIN LIST, used by every site that rewrites it.
   Written to a .part and renamed, so an interrupted write can never leave GoldHEN's loader a half
   file. Deliberately NOT modelled on fs_recv_write's unlink-then-rename: there is no need to remove
   the target first here, and doing so would open a window in which the console has no plugin list
   at all. open()/write(), not stdio, for the same measured reason as everywhere else in this
   payload - fopen() does not work from here. Returns 0 on success. */
/* THE OWNER'S PLUGIN LIST HAS ONE WRITER AT A TIME.
 *
 * Five functions mutate plugins.ini, each read-modify-write, and gh_ini_write_whole writes one fixed
 * "<path>.part" before renaming it. Two of them at once shared that scratch file: one's bytes landed in
 * the other's .part and whichever renamed last published a mixture. Three independent things trigger
 * those writes on a threaded server - the watcher when the console's content changes, the
 * /api/engine/agent route when the owner presses the control, and /api/cheat/rescan after filing new
 * files - and this is not our file: every other plugin the owner runs is listed in it, and it survives
 * a reboot and a re-jailbreak.
 *
 * Held across the whole read-modify-write, not just the write, because the read is where the decision
 * comes from. GH_INI_LOCK/UNLOCK rather than bare calls so the pairing is visible at every site. */
static pthread_mutex_t g_gh_ini_lock = PTHREAD_MUTEX_INITIALIZER;
#define GH_INI_LOCK()   pthread_mutex_lock(&g_gh_ini_lock)
#define GH_INI_UNLOCK() pthread_mutex_unlock(&g_gh_ini_lock)

static int gh_ini_write_whole(const char *buf, size_t len) {
    char part[sizeof(GH_PLUGINS_INI) + 8];
    snprintf(part, sizeof(part), "%s.part", GH_PLUGINS_INI);
    int f = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (f < 0) return -1;
    int ok = (write(f, buf, len) == (ssize_t)len);
    close(f);
    if (!ok) { unlink(part); return -1; }

    /* TRY THE REPLACE FIRST, and only clear the way if it is refused. rename() onto an existing file
       is NOT dependable here - this repo already records that, in the in-game agent's own
       write_atomic - and a host harness running the real function proved it: every toggle returned
       "could not be written" while the file itself was perfectly writable.
       The order matters and is not cosmetic. Unlinking first (which is what fs_recv_write does) would
       leave the console with NO plugin list for the width of that window, and a game launched inside
       it loads none of the owner's plugins. So the window is only ever opened when the platform has
       already refused to do it without one. */
    if (rename(part, GH_PLUGINS_INI) == 0) return 0;
    unlink(GH_PLUGINS_INI);
    if (rename(part, GH_PLUGINS_INI) == 0) return 0;
    unlink(part);
    return -1;
}

/* TURN GOLDHEN'S OWN LOAD BANNER OFF (and back on).
 *
 * With show_load_notification=true its loader draws "Loaded 1 plugin(s) 1. <name>" in its own gold
 * styling every time a game starts. That is GoldHEN telling the owner about OUR helper, in GoldHEN's
 * voice, and the shop says it better and at a more useful moment - when the agent is actually ready
 * rather than merely mapped. So the setting goes off while we are armed.
 *
 * IT IS RESTORED ON PURGE, because it is the OWNER'S setting and it governs every plugin they run,
 * not just ours. Measured on this console: plugins.ini lists no plugin but ours, so nothing else
 * loses a banner today - but that is a fact about today, not a licence.
 *
 * Only that one key is touched. Every other line, including any line longer than our buffers, is
 * copied through byte for byte, and the file is replaced whole through gh_ini_write_whole.
 * Returns 0 when the file now says what was asked, including when it already did.
 */
static int gh_set_load_notification_locked(int on) {
    long n = 0;
    char *doc = slurp(GH_PLUGINS_INI, &n);
    if (!doc) return -1;                        /* no file: nothing of ours is listed either */

    const char *KEY = "show_load_notification";
    const size_t KEYL = 22;                     /* strlen(KEY) */
    size_t cap = (size_t)n + 256;
    char *out = (char *)malloc(cap);
    if (!out) { free(doc); return -1; }
    size_t w = 0;
    int wrote = 0;

    char *p = doc;
    while (p && *p) {
        char *e = strchr(p, '\n');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        const char *t = p;
        size_t skip = 0;
        while (skip < len && (*t == ' ' || *t == '\t')) { t++; skip++; }

        int is_key = (len - skip > KEYL) && !strncmp(t, KEY, KEYL);
        if (is_key) {
            /* Only the assignment, never a comment that happens to mention the key. */
            const char *q = t + KEYL;
            while (*q == ' ' || *q == '\t') q++;
            if (*q != '=') is_key = 0;
        }
        if (w + len + 64 > cap) { free(out); free(doc); return -1; }
        if (is_key && !wrote) {
            /* KEEP THE LINE'S OWN ENDING. plugins.ini belongs to the owner and they may have edited
               it on a PC, so a CRLF file must not come back with one LF line in the middle of it. */
            int cr = (len > 0 && p[len - 1] == '\r');
            int k = snprintf(out + w, cap - w, "%s=%s%s\n",
                             KEY, on ? "true" : "false", cr ? "\r" : "");
            if (k > 0) w += (size_t)k;
            wrote = 1;
        } else if (!is_key) {
            memcpy(out + w, p, len);
            w += len;
            out[w++] = '\n';
        }
        /* A SECOND assignment of the same key is dropped: one key, one value. */
        p = e ? e + 1 : NULL;
    }
    if (!wrote) {
        /* No such key anywhere. Adding it needs a section to put it in. */
        int k = snprintf(out + w, cap - w, "\n[settings]\n%s=%s\n", KEY, on ? "true" : "false");
        if (k > 0) w += (size_t)k;
    }

    /* WRITE ONLY IF SOMETHING ACTUALLY CHANGED, decided by comparing the whole result with what was
       read - not by guessing from a line's length, which is what the first version did and why it
       silently wrote nothing: with a trailing CR, "=true" and "=false" are the same number of bytes. */
    int rc = 0;
    if (w != (size_t)n || memcmp(out, doc, w) != 0) rc = gh_ini_write_whole(out, w);
    free(out);
    free(doc);
    return rc;
}

/* WHAT THE PLUGIN LIST ACTUALLY SAYS. There can be more than one mention of our line, and the old
   version of this function did not believe that: it stopped at the first one, with the comment "the
   first mention decides; we only ever write one". agent_enable_for_title falsifies that - it appends
   a SECOND mention inside a [TID] section and leaves the one under [default] commented out - so a
   per-title arm was invisible to the only instrument this project has for "is the helper wired into
   a game", and /api/engine/agent reported it as off. */
typedef struct {
    int  listed;            /* our line appears at all, commented or not */
    int  armed_default;     /* an uncommented mention in [default] (or before any section) */
    int  armed_titles;      /* how many title sections carry an uncommented mention */
    char titles[160];       /* those title ids, comma separated, truncated if there are many */
} agent_arm_t;

static void agent_scan_arm(agent_arm_t *a) {
    memset(a, 0, sizeof(*a));
    long n = 0;
    char *doc = slurp(GH_PLUGINS_INI, &n);
    if (!doc) return;

    char sec[40] = {0};                  /* the section we are inside; empty means none yet */
    char *p = doc;
    while (p && *p) {
        char *e = strchr(p, '\n');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        char line[512];
        size_t cl = len < sizeof(line) - 1 ? len : sizeof(line) - 1;
        memcpy(line, p, cl);
        line[cl] = 0;

        const char *t = line;
        while (*t == ' ' || *t == '\t') t++;
        if (*t == '[') {
            size_t k = 0;
            const char *q = t + 1;
            while (*q && *q != ']' && k < sizeof(sec) - 1) sec[k++] = *q++;
            sec[k] = 0;
        } else if (agent_line_is_ours(line)) {
            a->listed = 1;
            if (*t != ';') {
                if (!sec[0] || !strcmp(sec, "default")) {
                    a->armed_default = 1;
                } else {
                    a->armed_titles++;
                    size_t have = strlen(a->titles);
                    /* + 2 for the separator, + 1 for the terminator */
                    if (have + strlen(sec) + 3 < sizeof(a->titles))
                        snprintf(a->titles + have, sizeof(a->titles) - have,
                                 "%s%s", have ? "," : "", sec);
                }
            }
        }
        p = e ? e + 1 : NULL;
    }
    free(doc);
}

/* 1 listed and active anywhere, 0 listed but every mention commented out, -1 not listed at all.
   Derived from the full scan so a per-title arm counts. */
static int agent_enabled_state(void) {
    agent_arm_t a;
    agent_scan_arm(&a);
    if (a.armed_default || a.armed_titles) return 1;
    return a.listed ? 0 : -1;
}

/* Returns 0 on success. Writes the file back with our line commented or uncommented, adding it
   under [default] when it was not there at all. */
static int agent_set_enabled_locked(int want_on) {
    long n = 0;
    char *doc = slurp(GH_PLUGINS_INI, &n);

    /* No file yet: write a minimal one that says what it is. GoldHEN creates its own when it
       first runs, and this is the same shape - its loader logs the section names it reads. */
    if (!doc) {
        /* open()/write(), not stdio. Every other writer in this file does the same, and that is
           not a style preference: fopen() does not work from this payload. Measured - the first
           version of this function refused every toggle with "could not be written", on a file
           that /api/fs/write had created a minute earlier without trouble. */
        char first[700];
        int fl = snprintf(first, sizeof(first),
            "; GoldHEN plugin loader configuration.\n"
            "; [default] loads a plugin into every game; [CUSAxxxxx] loads it into one title.\n"
            "; Lines starting with ; are comments.\n\n"
            "[settings]\nshow_load_notification=true\n\n"
            "[default]\n"
            "; PKG MUTANT SHOP in-game helper - what lets cheats be switched on while a PS4 game\n"
            "; is running. It does nothing until the shop asks it to.\n"
            "%s%s\n", want_on ? "" : "; ", PMS_AGENT_PRX);
        if (fl <= 0 || fl >= (int)sizeof(first)) return -1;
        return gh_ini_write_whole(first, (size_t)fl);
    }

    /* HEADROOM, AND ENOUGH OF IT. This was n + 512 while the loop below refused to continue once
       within 600 bytes of the end - so every file larger than about 570 bytes declared itself
       truncated and the toggle refused, on a 660-byte config. The margin has to exceed the guard,
       not sit under it. 8 KB is far more than the one line this ever adds. */
    size_t cap = (size_t)n + 8192;
    char *out = (char *)malloc(cap);
    if (!out) { free(doc); return -1; }
    size_t w = 0;
    int seen = 0, truncated = 0;

    /* PASS ONE: does a [default] section exist, and where does it END? The previous version tried to
       do this inline with one variable and could not: it recomputed in_default from the CURRENT line
       and then tested `in_default && *t == '['` - i.e. !X && X - so the branch that inserts our line
       inside an existing [default] was unreachable, `wrote_in_default` was dead, and control always
       reached the "if (!seen)" tail, which appends a SECOND "[default]" at the end of the file.
       default_end is the offset of the line that ends the section: the next section header, or the
       end of the document. */
    size_t default_end = 0;
    int    has_default = 0;
    {
        char *p = doc;
        int in_default = 0;
        while (p && *p) {
            char *e = strchr(p, '\n');
            size_t len = e ? (size_t)(e - p) : strlen(p);
            const char *t = p;
            size_t skip = 0;
            while (skip < len && (*t == ' ' || *t == '\t')) { t++; skip++; }
            if (skip < len && *t == '[') {
                int is_default = (len - skip >= 9) && !strncmp(t, "[default]", 9);
                if (in_default && !is_default) {          /* the section ends at THIS line */
                    default_end = (size_t)(p - doc);
                    break;
                }
                in_default = is_default;
                if (is_default) has_default = 1;
            }
            p = e ? e + 1 : NULL;
            if (!p || !*p) { if (in_default) default_end = (size_t)n; }
        }
        if (has_default && !default_end) default_end = (size_t)n;
    }

    /* PASS TWO: copy the document through verbatim, replace our own line with the wanted spelling,
       and insert our line at default_end when it was not already there.
       THE ORIGINAL SPAN IS WHAT GETS EMITTED, not the bounded copy. Both writers used to clamp every
       line into char[512] and then write the CLAMPED text, so any line of 512 bytes or more in the
       owner's config permanently lost its tail. The bounded copy survives for the TESTS only
       (whitespace skip, agent_line_is_ours), which need a terminated string. Because memcpy is not
       self-bounding the way snprintf was, the headroom guard now runs BEFORE the copy and accounts
       for the line's real length. */
    char *p = doc;
    while (p && *p) {
        char *e = strchr(p, '\n');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        char line[512];
        size_t cl = len < sizeof(line) - 1 ? len : sizeof(line) - 1;
        memcpy(line, p, cl);
        line[cl] = 0;

        if (w + len + 1 + 1024 > cap) { truncated = 1; break; }   /* refuse to truncate - see below */

        if (!seen && has_default && (size_t)(p - doc) == default_end) {
            w += (size_t)snprintf(out + w, cap - w, "%s%s\n", want_on ? "" : "; ", PMS_AGENT_PRX);
            seen = 1;
        }

        if (agent_line_is_ours(line)) {
            seen = 1;
            w += (size_t)snprintf(out + w, cap - w, "%s%s\n", want_on ? "" : "; ", PMS_AGENT_PRX);
        } else {
            memcpy(out + w, p, len);
            w += len;
            out[w++] = '\n';
        }
        p = e ? e + 1 : NULL;
    }
    if (!seen && !truncated) {
        /* Either [default] runs to the end of the file, or there is no [default] at all. */
        if (!has_default)
            w += (size_t)snprintf(out + w, cap - w, "\n[default]\n");
        w += (size_t)snprintf(out + w, cap - w, "%s%s\n", want_on ? "" : "; ", PMS_AGENT_PRX);
    }

    /* WRITE ONLY A WHOLE FILE. `truncated` is set by the one break that can leave the copy
       incomplete; anything else means the walk finished. Testing `p == NULL` instead - which is
       what this did first - is wrong for the ordinary case of a file ending in a newline, because
       the loop then exits on *p == 0 with p still pointing at the terminator. The symptom was a
       toggle that refused every time and said the file could not be written, on a file that was
       perfectly writable. It failed safe, which is why the config survived it. */
    int rc = -1;
    if (!truncated) rc = gh_ini_write_whole(out, w);
    free(out);
    free(doc);
    /* The [default] path gets the same treatment as the per-title one: while our helper is armed our
       own message is the only one the owner sees. Switching OFF does not touch the setting here -
       agent_purge restores it, and that is the single place responsible for giving it back. */
    if (rc == 0 && want_on) (void)gh_set_load_notification_locked(0);  /* we hold the ini lock */
    return rc;
}

/* Put the load-safe agent on the console, byte for byte, WITHOUT listing it.
 *
 * Writing the file is safe on its own: nothing loads a plugin that is not named in plugins.ini, so
 * a console that merely carries the .prx behaves exactly as one that does not. Listing is a
 * separate, explicit step (agent_enable_for_title / agent_set_enabled). Content-compared so a
 * rebuilt shop refreshes a rebuilt agent, and written .part-then-rename so GoldHEN's loader can
 * never read a half-written file. Returns 0 if the file is present and current afterwards. */
/* Is something listening on one of OUR OWN ports? Used only to colour a tile.

   :9090 IS NEVER PASSED TO THIS. GoldHEN's payload loader stops listening when a connection is
   opened and closed without an ELF being posted, so the POST is the probe and there is no other
   way to ask. Every caller here asks about a service port (2121 and the like), never the loader. */
/* Declared here because cheat_core.h, which defines it, is included further down than the scan
   below needs it - the same forward-declaration the PS5 build keeps at the top of its file. */
static int path_ext_is(const char *path, const char *ext);


/* The title ids this console already has, so the panel can say "Installed" without a PC.

   Read from the folder the console itself keeps per installed title - appmeta on the PS5, the app
   directory on the PS4 - because that is the same evidence the library trusts. The page matches
   these against the catalogue's title_id; nothing here needs to know what a homebrew is. */
/* The title ids this console really has, so the panel can say "Installed" without a PC.

   APPMETA IS ARTWORK, NOT EVIDENCE. This used to list the directories under /user/appmeta, and a
   folder there means only that the console once had the icon for a title: it is written early, it
   is left behind by an install that failed, and it survives a database reset. That is not a
   hypothesis - it is how 53 titles were once reported installed on a console that held none of
   them, and it is why pressing Install on a homebrew the console did not have could answer
   "Installed" seconds later while nothing had been downloaded.

   The proof used here is the one the rest of this file already trusts, and the one the install
   lane waits for before it will call a job done: the title's own **app.pkg, with bytes**, under a
   root a game can actually live on. An install that was registered and never completed has no such
   file, so it no longer counts. */
/* Is an install actually in flight? `active` outlives the job so its outcome can still be shown,
   so "running" is active AND not in a terminal state - the same test the accept gate uses. */
static int job_running(void) {
    return g_job.active && strcmp(g_job.state, "installed") && strcmp(g_job.state, "error");
}

static int app_ids_json(char *out, size_t outsz) {
    int n = 0;
    out[0] = 0;
    for (int r = 0; APP_ROOTS[r] && n < (int)outsz - 40; r++) {
        DIR *d = opendir(APP_ROOTS[r]);
        if (!d) continue;
        struct dirent *de;
        while ((de = readdir(d)) && n < (int)outsz - 40) {
            if (de->d_name[0] == '.') continue;
            size_t L = strlen(de->d_name);
            if (L < 6 || L > 12) continue;
            /* installed_app_pkg() is this file's own definition of "really installed". */
            if (installed_app_pkg(de->d_name) <= 0) continue;
            n += snprintf(out + n, outsz - (size_t)n, "%s\"%s\"", n ? "," : "", de->d_name);
        }
        closedir(d);
    }
    return n;
}

static const char *HB_ROOTS[] = {
    HB_DIR,
    "/mnt/usb0/homebrews", "/mnt/usb1/homebrews", "/mnt/usb2/homebrews", "/mnt/usb3/homebrews",
    "/mnt/usb0", "/mnt/usb1", "/mnt/usb2", "/mnt/usb3",
    NULL
};

/* Every homebrew package this console can reach BY ITSELF, as {"n":name,"s":bytes,"p":path}.

   THIS IS WHAT "NO PC AT ALL" ACTUALLY NEEDS. The owner installs this app from a USB stick or over
   FTP with no companion running, so the packages have to be findable the same way: drop them on a
   stick, or in our data folder, and the panel lists them.

   MATCHED BY SIZE, NOT BY NAME. The page pairs these with the catalogue on the exact byte count,
   because the owner renames files and a length is a fact about the contents rather than the label.
   Two homebrews would have to be byte-for-byte the same size to be confused, and the catalogue
   carries a sha256 for anything that ever has to be certain.

   Only the top level of each root is read. A full walk of a 2 TB drive, on an accept loop, for a
   panel that redraws every six seconds, is not a trade worth making. */
static int hb_list_json(char *out, size_t outsz) {
    int n = 0;
    out[0] = 0;
    for (int r = 0; HB_ROOTS[r] && n < (int)outsz - 200; r++) {
        DIR *d = opendir(HB_ROOTS[r]);
        if (!d) continue;
        struct dirent *de;
        while ((de = readdir(d)) && n < (int)outsz - 200) {
            if (de->d_name[0] == '.' || !path_ext_is(de->d_name, ".pkg")) continue;
            char full[700];
            snprintf(full, sizeof(full), "%s/%s", HB_ROOTS[r], de->d_name);
            struct stat st;
            if (stat(full, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size <= 0) continue;
            n += snprintf(out + n, outsz - (size_t)n,
                          "%s{\"n\":\"%s\",\"s\":%lld,\"p\":\"%s\"}",
                          n ? "," : "", de->d_name, (long long)st.st_size, full);
        }
        closedir(d);
    }
    return n;
}

/* The comparable stem of a payload filename: lowercase, no extension, no -ps4/-ps5, no _v1.2.3.

   The page asks for a payload using the name the OWNER sees, and this ELF carries it under the
   stable catalogue id - a versioned path in an .incbin breaks the build the first time an upstream
   release lands. This is what makes the two names meet. */
static void p4_stem(const char *name, char *out, size_t outsz) {
    size_t n = 0;
    for (const char *p = name; *p && n + 1 < outsz; p++)
        out[n++] = (char)((*p >= 'A' && *p <= 'Z') ? *p - 'A' + 'a' : *p);
    out[n] = 0;
    if (n > 4 && !strcmp(out + n - 4, ".elf")) { n -= 4; out[n] = 0; }
    if (n > 4 && (out[n - 4] == '-' || out[n - 4] == '_') && out[n - 3] == 'p' && out[n - 2] == 's'
        && (out[n - 1] == '4' || out[n - 1] == '5')) { n -= 4; out[n] = 0; }
    for (size_t i = n; i-- > 0;) {
        char c = out[i];
        if ((c >= '0' && c <= '9') || c == '.') continue;
        /* STEP OVER THE 'v'. The walk back over the version digits used to stop here and break,
           so the branch below that was written to skip it could never be reached and nothing was
           ever stripped: "pldmgr_v0.5.2.elf" kept its version, while the page's phbStem - the
           same rule in JavaScript - removed it. The two then disagreed about what a file is, which
           is why a payload named that way read as stopped and why a downloaded update did not
           replace the previous copy beside it. Measured on the PS5, three webkit-autoloader files
           in one folder. */
        if ((c == 'v' || c == 'V') && i > 0 && i + 1 < n &&
            (out[i - 1] == '_' || out[i - 1] == '-')) { out[i - 1] = 0; break; }
        if ((c == '_' || c == '-') && i + 1 < n) {
            size_t j = i + 1;
            if (out[j] == 'v') j++;
            if (j < n && out[j] >= '0' && out[j] <= '9') out[i] = 0;
        }
        break;
    }
}

/* Is a UDP port already taken? That is the only honest test for a service that listens on UDP and
   replies to nobody.

   nanodns is exactly that. A TCP connect to :53 proves nothing, and a DNS query sent to the console
   from the LAN gets no answer even when nanodns IS running - measured on both consoles, against its
   own spoofing domains as well as ordinary ones. What CAN be observed is that the port is occupied:
   bind it, and if the bind is refused because the address is in use, something else holds it.

   NO SO_REUSEADDR, deliberately: with it the bind would succeed alongside the running server and
   the test would report "free" for ever, which is the same shape of permanently-wrong answer the
   9021-vs-10101 mix-up gave. The socket is closed immediately either way, so a port that really was
   free is left exactly as it was found.

   Both 127.0.0.1 and 0.0.0.0 are tried, because a server bound to one does not always conflict with
   the other, and either conflict is proof. */
static int udp_port_taken(int port) {
    if (port <= 0) return 0;
    const char *addrs[2] = { "127.0.0.1", "0.0.0.0" };
    for (int i = 0; i < 2; i++) {
        int s = socket(AF_INET, SOCK_DGRAM, 0);
        if (s < 0) continue;
        struct sockaddr_in a;
        memset(&a, 0, sizeof(a));
        a.sin_family = AF_INET;
        a.sin_port = htons((unsigned short)port);
        a.sin_addr.s_addr = inet_addr(addrs[i]);
        int rc = bind(s, (struct sockaddr *)&a, sizeof(a));
        int err = errno;
        close(s);
        if (rc != 0 && (err == EADDRINUSE || err == EACCES)) return 1;
    }
    return 0;
}

static int p4_port_open(int port) {
    if (port <= 0) return 0;
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return 0;
    struct timeval tv; tv.tv_sec = 1; tv.tv_usec = 0;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((unsigned short)port);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    int rc = connect(s, (struct sockaddr *)&a, sizeof(a));
    close(s);
    return rc == 0;
}

/* Write the payloads this ELF carries into PB_DIR.

   Content-compared before writing, exactly like agent_deploy below: a rebuilt shop refreshes a
   rebuilt payload and an unchanged one costs one read. Written through <path>.part and renamed,
   because a half-written ELF that GoldHEN then loads is a crash with no explanation. */
/* A copy of one bundled payload in /data/payloads, where everyone else keeps them.

   Best effort on purpose: a full disk, a read-only mount or a folder somebody deleted must never
   stop the shop from starting, and nothing in this app ever reads from here - PB_DIR is ours and
   stays authoritative. This exists so that somebody installing from a USB stick or over FTP, with
   no PC anywhere, finds the folder already there with the right name and something in it. */
static void pb_mirror(const p4pb_entry_t *e, size_t len) {
    char mir[600], mpart[640];
    snprintf(mir, sizeof(mir), "%s/%s", GH_PAYLOAD_DIR, e->filename);
    struct stat ms;
    if (stat(mir, &ms) == 0 && (size_t)ms.st_size == len) return;
    snprintf(mpart, sizeof(mpart), "%s.part", mir);
    int mf = open(mpart, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (mf < 0) return;
    size_t ml = len;
    const unsigned char *mp = e->data;
    while (ml) {
        int w = (int)write(mf, mp, ml);
        if (w <= 0) break;
        mp += w; ml -= (size_t)w;
    }
    close(mf);
    if (ml) { unlink(mpart); return; }
    if (rename(mpart, mir) != 0) { unlink(mir); if (rename(mpart, mir) != 0) unlink(mpart); }
}

static int pb_deploy_all(void) {
    mkdir("/data", 0777);
    mkdir(SHOP_DATA_DIR, 0777);
    mkdir(PB_DIR, 0777);
    mkdir(HB_DIR, 0777);
    /* /data/payloads IS WHERE EVERYONE ELSE PUTS THEM, so it is made whether or not we are the
       ones filling it. Somebody installing this app from a USB stick or over FTP - no PC companion
       anywhere - drops an ELF into that folder with a file manager, and a folder that already
       exists with the right name is the difference between that working first time and them
       guessing. We mirror our own bundled payloads into it as well, so the folder is useful the
       moment it appears. It is NOT where we load from: PB_DIR is ours and stays authoritative. */
    mkdir(GH_PAYLOAD_DIR, 0777);
    int bad = 0;
    for (int i = 0; i < PS4_PAYLOAD_BUNDLE_COUNT; i++) {
        const p4pb_entry_t *e = &PS4_PAYLOAD_BUNDLE[i];
        size_t len = (size_t)(e->end - e->data);
        char full[512], part[544];
        snprintf(full, sizeof(full), "%s/%s", PB_DIR, e->filename);
        /* THE MIRROR IS ITS OWN QUESTION. This used to sit after the early `continue` below, so on
           every boot where our own copy was already correct - which is every boot after the first -
           the mirror was never written at all, and /data/payloads stayed empty. Measured on this
           console: PB_DIR had both payloads and /data/payloads did not exist. Do it first, and do
           it whether or not the primary needed touching. */
        pb_mirror(e, len);

        long have_n = 0;
        char *have = slurp(full, &have_n);
        if (have && (size_t)have_n == len && !memcmp(have, e->data, len)) { free(have); continue; }
        free(have);
        snprintf(part, sizeof(part), "%s.part", full);
        int fd = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0777);
        if (fd < 0) { bad++; continue; }
        size_t left = len;
        const unsigned char *p = e->data;
        while (left) {
            int w = (int)write(fd, p, left);
            if (w <= 0) break;
            p += w; left -= (size_t)w;
        }
        close(fd);
        if (left) { unlink(part); bad++; continue; }
        /* rename onto an existing file does not work on these consoles - try, then unlink+rename */
        if (rename(part, full) != 0) { unlink(full); if (rename(part, full) != 0) { unlink(part); bad++; } }

    }
    return bad ? -1 : 0;
}

/* Hand a payload to GoldHEN's loader on this console.

   THE POST IS THE PROBE. There is deliberately no connect-first check: opening :9090 and closing
   it again stops the loader listening, which is the standing rule everywhere else in this project
   that touches that port. */
static int gh_send_payload(const char *path, char *why, size_t wsz) {
    long n = 0;
    char *b = slurp(path, &n);
    if (!b || n <= 0) { free(b); snprintf(why, wsz, "that payload is not on this console"); return -1; }
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) { free(b); snprintf(why, wsz, "no socket"); return -1; }
    struct timeval tv; tv.tv_sec = 20; tv.tv_usec = 0;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons(9090);
    a.sin_addr.s_addr = inet_addr("127.0.0.1");
    if (connect(s, (struct sockaddr *)&a, sizeof(a)) != 0) {
        close(s); free(b);
        snprintf(why, wsz, "the payload loader is not listening");
        return -1;
    }
    int ok = (write_all(s, b, (size_t)n) == 0);
    close(s);
    free(b);
    if (!ok) { snprintf(why, wsz, "the payload loader did not take all of it"); return -1; }
    snprintf(why, wsz, "sent it to the payload loader");
    return 0;
}

static int agent_deploy(void) {
    size_t len = (size_t)(ab_pms_agent_prx_end - ab_pms_agent_prx);
    if (!len) return -1;

    long have_n = 0;
    char *have = slurp(PMS_AGENT_PRX, &have_n);
    if (have && (size_t)have_n == len && !memcmp(have, ab_pms_agent_prx, len)) {
        free(have);
        return 0;                                  /* already exactly this build */
    }
    free(have);

    mkdir("/data/GoldHEN", 0777);
    mkdir("/data/GoldHEN/plugins", 0777);
    char part[300];
    snprintf(part, sizeof(part), "%s.part", PMS_AGENT_PRX);
    int f = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (f < 0) return -1;
    int ok = (write(f, ab_pms_agent_prx, len) == (ssize_t)len);
    close(f);
    if (!ok) { unlink(part); return -1; }
    if (rename(part, PMS_AGENT_PRX) != 0) { unlink(part); return -1; }
    klog_puts("pms: load-safe in-game agent written (unlisted) to /data/GoldHEN/plugins\n");
    return 0;
}

/* List the agent for ONE TITLE only, by appending a [TID] section. This is the safe way to switch
 * the helper on: it modifies no existing line, so every other GoldHEN plugin the owner runs is left
 * exactly as it was, and only the named game loads our agent. Idempotent - if the title already has
 * our line (commented or not), it is set to on and nothing is duplicated. Returns 0 on success. */
static int agent_enable_for_title_locked(const char *tid) {
    if (!tid || !tid[0]) return -1;
    /* sanitise: a title id is CUSA/PPSA + digits, nothing that could inject a line */
    for (const char *c = tid; *c; c++)
        if (!((*c >= 'A' && *c <= 'Z') || (*c >= 'a' && *c <= 'z') ||
              (*c >= '0' && *c <= '9'))) return -1;

    if (agent_deploy() != 0) return -1;            /* the file must exist before it is listed */

    long n = 0;
    char *doc = slurp(GH_PLUGINS_INI, &n);
    size_t cap = (size_t)(doc ? n : 0) + 1024;
    char *out = (char *)malloc(cap);
    if (!out) { free(doc); return -1; }
    size_t w = 0;
    char want_sec[24];
    snprintf(want_sec, sizeof(want_sec), "[%s]", tid);

    /* Copy the whole file through, and inside an existing [TID] section make sure our line is
       present and uncommented. Track whether we saw the section at all. */
    int in_sec = 0, wrote_line = 0, saw_sec = 0;
    char *p = doc;
    while (p && *p) {
        char *e = strchr(p, '\n');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        char line[512];
        size_t cl = len < sizeof(line) - 1 ? len : sizeof(line) - 1;
        memcpy(line, p, cl); line[cl] = 0;
        const char *t = line; while (*t == ' ' || *t == '\t') t++;

        if (*t == '[') {
            /* leaving a section: if it was ours and we never wrote the line, add it now */
            if (in_sec && !wrote_line) {
                w += (size_t)snprintf(out + w, cap - w, "%s\n", PMS_AGENT_PRX);
                wrote_line = 1;
            }
            in_sec = !strncmp(t, want_sec, strlen(want_sec)) &&
                     (t[strlen(want_sec)] == 0 || t[strlen(want_sec)] == '\r');
            if (in_sec) saw_sec = 1;
        }
        /* Guard BEFORE the copy and for the line's real length: the pass-through is the original
           span, not the bounded copy, so it is not self-bounding the way snprintf was. */
        if (w + len + 1 + 256 > cap) { free(out); free(doc); return -1; }   /* refuse to truncate */

        if (in_sec && agent_line_is_ours(line)) {
            /* our line, possibly commented - write it uncommented, once */
            if (!wrote_line) { w += (size_t)snprintf(out + w, cap - w, "%s\n", PMS_AGENT_PRX); wrote_line = 1; }
        } else {
            memcpy(out + w, p, len);
            w += len;
            out[w++] = '\n';
        }
        p = e ? e + 1 : NULL;
    }
    if (in_sec && !wrote_line)      /* file ended still inside our section */
        w += (size_t)snprintf(out + w, cap - w, "%s\n", PMS_AGENT_PRX);
    if (!saw_sec)                   /* the title had no section at all - append one */
        w += (size_t)snprintf(out + w, cap - w, "\n%s\n%s\n", want_sec, PMS_AGENT_PRX);

    int rc = gh_ini_write_whole(out, w);
    free(out); free(doc);
    /* Our own message replaces GoldHEN's gold one while the helper is armed. Best-effort: a helper
       that is listed but whose banner could not be switched off is still a working helper. */
    if (rc == 0) (void)gh_set_load_notification_locked(0);             /* we hold the ini lock */
    return rc;
}

/* ---- AUTO-ARM ---------------------------------------------------------------------------------
 * Which installed titles should carry the helper, decided from facts we already have, and written
 * into GoldHEN's plugin list before any game starts - because its loader reads that file only when a
 * game launches, so a decision made after the launch is a decision made too late.
 */
#define AUTOARM_OFF_PATH  SHOP_DATA_DIR "/autoarm-off"
#define AUTOARM_SKIP_PATH SHOP_DATA_DIR "/autoarm-skip"
#define AUTOARM_MAX 64

static int autoarm_enabled(void) {
    struct stat st;
    return stat(AUTOARM_OFF_PATH, &st) != 0;      /* the file's presence is the "off" switch */
}

/* Titles autoarm must never touch. One id per line, '#' comments. Seeded once with the one title the
   owner has told us to leave alone; after that the file is theirs. */
static void autoarm_seed_skip(void) {
    struct stat st;
    if (stat(AUTOARM_SKIP_PATH, &st) == 0) return;
    static const char seed[] =
        "# Titles PKG MUTANT SHOP will NEVER switch the in-game helper on for by itself.\n"
        "# One title id per line. Lines starting with # are ignored. Delete a line to allow it.\n"
        "#\n"
        "# CUSA23827 is here because the owner said they do not run it, to avoid a ban - putting our\n"
        "# code inside an online game is the risk they are avoiding. Their call, not ours.\n"
        "CUSA23827\n";
    int f = open(AUTOARM_SKIP_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (f < 0) return;
    (void)!write(f, seed, sizeof(seed) - 1);
    close(f);
}

static int autoarm_skipped(const char *tid) {
    long n = 0;
    char *doc = slurp(AUTOARM_SKIP_PATH, &n);
    if (!doc) return 0;
    int hit = 0;
    char *p = doc;
    while (p && *p && !hit) {
        char *e = strchr(p, '\n');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        const char *t = p;
        size_t skip = 0;
        while (skip < len && (*t == ' ' || *t == '\t')) { t++; skip++; }
        if (skip < len && *t != '#') {
            size_t k = 0;
            while (skip + k < len && t[k] > 32 && t[k] != '#') k++;
            if (k && k == strlen(tid) && !strncmp(t, tid, k)) hit = 1;
        }
        p = e ? e + 1 : NULL;
    }
    free(doc);
    return hit;
}

/* Rewrite GoldHEN's plugin list so our line is present and uncommented in EXACTLY the wanted [TID]
   sections, and nowhere else among the title sections.
 *
 * [default] IS PASSED THROUGH UNTOUCHED. The blanket form is a deliberate, separate decision (Settings)
 * and this must never reach for it - autoarm's whole argument is that a game with nothing should not
 * load our module at all.
 * Every other line in the file is copied byte for byte, including one longer than our buffers. The file
 * is written only if the result differs from what was read. */
static int agent_autoarm_apply_locked(const char wanted[][16], int nwanted, int *out_armed) {
    if (out_armed) *out_armed = 0;
    long n = 0;
    char *doc = slurp(GH_PLUGINS_INI, &n);
    if (!doc) return -1;

    size_t cap = (size_t)n + (size_t)AUTOARM_MAX * 80 + 1024;
    char *out = (char *)malloc(cap);
    if (!out) { free(doc); return -1; }
    size_t w = 0;

    /* DOES THIS FILE USE CRLF? Decided once, from its first line ending, and applied to every line we
       emit ourselves. Without this our inserted line would be the one odd line out in a config the
       owner edited on a PC - and it would compare unequal on every boot and be rewritten for ever,
       which is the same pointless churn the purge gate was fixed to avoid. */
    const char *NL = "\n";
    {
        const char *nl = strchr(doc, '\n');
        if (nl && nl > doc && nl[-1] == '\r') NL = "\r\n";
    }

    char written[AUTOARM_MAX];      /* our line emitted inside that title's section */
    char seen[AUTOARM_MAX];         /* the title has a section at all */
    memset(written, 0, sizeof(written));
    memset(seen, 0, sizeof(seen));

    int cur = -1;                   /* index into wanted[] of the section we are inside, or -1 */
    int in_default = 0;

    char *p = doc;
    while (p && *p) {
        char *e = strchr(p, '\n');
        size_t len = e ? (size_t)(e - p) : strlen(p);
        char line[512];
        size_t cl = len < sizeof(line) - 1 ? len : sizeof(line) - 1;
        memcpy(line, p, cl);
        line[cl] = 0;
        const char *t = line;
        while (*t == ' ' || *t == '\t') t++;

        if (w + len + 128 > cap) { free(out); free(doc); return -1; }

        if (*t == '[') {
            /* Leaving a wanted section without having written our line: write it now. */
            if (cur >= 0 && !written[cur]) {
                w += (size_t)snprintf(out + w, cap - w, "%s%s", PMS_AGENT_PRX, NL);
                written[cur] = 1;
            }
            in_default = !strncmp(t, "[default]", 9);
            cur = -1;
            for (int i = 0; i < nwanted; i++) {
                size_t wl = strlen(wanted[i]);
                if (!strncmp(t + 1, wanted[i], wl) && t[1 + wl] == ']') { cur = i; seen[i] = 1; break; }
            }
            memcpy(out + w, p, len); w += len; out[w++] = '\n';
        } else if (in_default) {
            /* Untouched, whatever it says. */
            memcpy(out + w, p, len); w += len; out[w++] = '\n';
        } else if (agent_line_is_ours(line)) {
            if (cur >= 0 && !written[cur]) {
                w += (size_t)snprintf(out + w, cap - w, "%s%s", PMS_AGENT_PRX, NL);
                written[cur] = 1;
            }
            /* else: dropped - a stale arm in a title we no longer want, or a duplicate. */
        } else {
            /* The original span including any trailing '\r', then the newline that terminated it:
               a CRLF line comes back out as CRLF without this needing to know. */
            memcpy(out + w, p, len); w += len; out[w++] = '\n';
        }
        p = e ? e + 1 : NULL;
    }
    if (cur >= 0 && !written[cur]) {
        w += (size_t)snprintf(out + w, cap - w, "%s%s", PMS_AGENT_PRX, NL);
        written[cur] = 1;
    }
    for (int i = 0; i < nwanted; i++) {
        if (seen[i]) continue;
        w += (size_t)snprintf(out + w, cap - w, "%s[%s]%s%s%s",
                              NL, wanted[i], NL, PMS_AGENT_PRX, NL);
        written[i] = 1;
    }

    int armed = 0;
    for (int i = 0; i < nwanted; i++) if (written[i]) armed++;
    if (out_armed) *out_armed = armed;

    int rc = 0;
    if (w != (size_t)n || memcmp(out, doc, w) != 0) rc = gh_ini_write_whole(out, w);
    free(out);
    free(doc);
    return rc;
}

/* ---- THE FOUR WRAPPERS -----------------------------------------------------------------------
 * Every write of the owner's plugin list goes through one of these, and the lock is held across the
 * whole read-modify-write because the read is where the decision comes from. The _locked forms exist
 * for the calls BETWEEN writers - agent_set_enabled and agent_enable_for_title both set the loader
 * notification from inside their own update - which would otherwise lock a non-recursive mutex twice.
 */
static int gh_set_load_notification(int on) {
    GH_INI_LOCK();
    int rc = gh_set_load_notification_locked(on);
    GH_INI_UNLOCK();
    return rc;
}

static int agent_set_enabled(int want_on) {
    GH_INI_LOCK();
    int rc = agent_set_enabled_locked(want_on);
    GH_INI_UNLOCK();
    return rc;
}

static int agent_enable_for_title(const char *tid) {
    GH_INI_LOCK();
    int rc = agent_enable_for_title_locked(tid);
    GH_INI_UNLOCK();
    return rc;
}

static int agent_autoarm_apply(const char wanted[][16], int nwanted, int *out_armed) {
    GH_INI_LOCK();
    int rc = agent_autoarm_apply_locked(wanted, nwanted, out_armed);
    GH_INI_UNLOCK();
    return rc;
}


/* TAKE THE IN-GAME PLUGIN BACK OFF THE CONSOLE.
 *
 * We used to write pms-agent.prx into /data/GoldHEN/plugins and list it under [default], so that
 * GoldHEN's loader put it inside every game and the shop could reach that game's memory through
 * it. IT HANGS GAMES: with it listed, Dark Souls II sticks for ever on the PS4's "Please wait..."
 * screen - measured twice on the owner's console - and the plugin's own socket never comes up, so
 * it is hanging at or before plugin_load rather than misbehaving afterwards.
 *
 * The architecture was the mistake and not the detail. Our PS5 payload reaches a running game's
 * memory from OUTSIDE it and has never destabilised one; code of ours INSIDE somebody's game can
 * hang it, crash it or corrupt it, and all three look like a broken app to the person holding the
 * controller.
 *
 * WHY THIS IS AN ACTIVE REMOVAL AND NOT JUST "WE STOPPED WRITING IT". Both the .prx and the line
 * in plugins.ini survive a reboot AND a re-jailbreak. A console left with the helper listed would
 * hang on the next game launch with nothing of ours running at all, and there would be nothing to
 * press to fix it. Undoing our own change has to be something this payload does on its own.
 *
 * Returns the number of things it removed, or 0 when there was nothing of ours left.
 */
static int agent_purge(void) {
    int did = 0;
    agent_info_forget();          /* nothing cached about a helper we are taking away is still true */

    /* Take our line out of GoldHEN's plugin list first. If anything below fails, the console is
       still safe: an unlisted plugin is never loaded, whether or not its file is still there. */
    /* == 1, NOT >= 0. A line we already commented out on an earlier boot is harmless - an unlisted
       plugin is never loaded - so re-entering here bought nothing and cost something: it rewrote the
       owner's plugin list to byte-identical content on EVERY payload load, while klog announced
       "unlisted the in-game plugin" and startup_extras reported "purge removed 1 item(s)" for ever.
       Measured in a host harness carved from these same functions: boot 1 state=1, 213 -> 215 bytes,
       did=1; boots 2 to 5 state=0, and with the old gate the file went 215 -> 215 BYTE-IDENTICAL
       with did=1 every time. Reporting a removal that did not happen is how a real failure hides. */
    if (agent_enabled_state() == 1) {
        if (agent_set_enabled(0) == 0) {
            klog_puts("pms: unlisted the in-game plugin in GoldHEN's plugins.ini\n");
            did++;
        }
    }
    /* GIVE THE OWNER THEIR BANNER BACK. Arming switches GoldHEN's load notification off so our own
       message is the only one; nothing of ours is listed any more, so the setting is not ours to keep
       holding down. Unconditional and best-effort: it is one key, and it is theirs. */
    (void)gh_set_load_notification(1);

    struct stat st;
    if (stat(PMS_AGENT_PRX, &st) == 0) {
        if (unlink(PMS_AGENT_PRX) == 0) {
            klog_puts("pms: removed /data/GoldHEN/plugins/pms-agent.prx\n");
            did++;
        }
    }
    /* A .part left behind by an interrupted write of an earlier build. */
    char part[300];
    snprintf(part, sizeof(part), "%s.part", PMS_AGENT_PRX);
    if (stat(part, &st) == 0 && unlink(part) == 0) did++;

    /* The marker that used to record "this console has been set up once". Nothing reads it any
       more, and leaving it would quietly change the meaning of a future first install. */
    if (stat(AGENT_SETUP_MARK, &st) == 0 && unlink(AGENT_SETUP_MARK) == 0) did++;

    return did;
}

/* ================= THE FILE CHANNEL TO THE IN-GAME AGENT =================
 *
 * The agent used to serve memory over a loopback socket. It does not any more, and that is the
 * whole point: opening a listening socket inside a game process is the ONE thing our plugin did
 * that no plugin known to load cleanly on this console does, and every edition that did it broke
 * games - ending with a console that needed a reboot and a re-jailbreak. Files are a mechanism the
 * console's own plugins demonstrate is safe in exactly this context.
 *
 * One request at a time, written to a ".tmp" and renamed into place so neither side can read a
 * half-written file:
 *     cmd.bin   WE write it, the agent reads and deletes it
 *     res.bin   the AGENT writes it, we read and delete it
 *
 *     request   "PMSC" seq:u32 op:u8 pad:u8[3] addr:u64 len:u32   (24 bytes) [+len for WRITE]
 *     response  "PMSR" seq:u32 st:u8 pad:u8[3] len:u32            (16 bytes) [+len for READ]
 *
 * `seq` is echoed so a reply can never be taken for the answer to a different question. `len` is
 * the length the request is ABOUT - for a read nothing follows the header. The agent polls twice a
 * second, so a round trip settles well inside the timeout below.
 * ======================================================================================= */
#define AGENT_OP_PING   1
#define AGENT_OP_STATUS 2
#define AGENT_OP_READ   3
#define AGENT_OP_WRITE  4
/* ONE WIRE CONSTANT, THREE COPIES - they move together or the channel mismatches. The others are
   PMS_MAX_CHUNK (ps4-app/plugin/source/main.c) and CHUNK (ps4-app/plugin/test/harness.c), and the
   PC test compares a real exchange against the real agent, so a drift shows up as a red suite.
   4 KiB, not the 64 KiB this used to be: the agent sizes two static buffers from it and so reserved
   128 KiB inside somebody's game for a path that never moves more than 256 bytes. */
#define AGENT_CHUNK   (4 * 1024)
_Static_assert(AGENT_CHUNK >= 420, "the agent's OP_STATUS answer can be 420 bytes");
#define AGENT_REQ_HDR 24
#define AGENT_RES_HDR 16
#define AGENT_REQ_MAGIC "PMSC"
#define AGENT_RES_MAGIC "PMSR"
/* Generous next to the agent's half-second poll, short enough that a panel never feels hung: the
   answer either arrives within a couple of polls or the agent is not in the game. */
#define AGENT_WAIT_MS 2500
#define AGENT_PORT    0          /* kept so /api/engine/agent's reply shape does not change */

/* The same two candidates the agent tries, in the same order. Whichever it could write in is the
   one its files appear in, so we simply look in both. */
#define AGENT_DIR "/data/pkg-mutant-shop/agent"
static const char *const AGENT_DIRS[] = {
    AGENT_DIR,
    "/data/GoldHEN/pms-agent",
    NULL
};

/* HOW STALE IS TOO STALE, in one place. The agent rewrites alive.bin every couple of seconds while
   it is inside a running game, so its mtime answers "is anyone home" without asking anything. Both
   readers - agent_req before every exchange, and the watcher once every two seconds - use this
   number, because two copies of it would drift and the symptom would be a panel that disagrees with
   a toast. */
#define AGENT_ALIVE_MAX_AGE 8

/* IS AN AGENT ALIVE IN A GAME RIGHT NOW - one stat per candidate directory, no round trip at all.
   This is what makes "no agent" cost a tenth of a second instead of waiting out the full timeout on
   a route the panel polls. */
static int agent_alive_fresh(void) {
    for (int d = 0; AGENT_DIRS[d]; d++) {
        char alive[160];
        snprintf(alive, sizeof(alive), "%s/alive.bin", AGENT_DIRS[d]);
        struct stat st;
        if (stat(alive, &st) != 0) continue;
        long long age = (long long)time(NULL) - (long long)st.st_mtime;
        if (age < 0) age = 0;
        if (age <= AGENT_ALIVE_MAX_AGE) return 1;
    }
    return 0;
}

static int agent_write_atomic(const char *path, const char *tmp,
                              const unsigned char *buf, size_t n) {
    int f = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (f < 0) return -1;
    size_t done = 0;
    while (done < n) {
        ssize_t w = write(f, buf + done, n - done);
        if (w <= 0) { close(f); unlink(tmp); return -1; }
        done += (size_t)w;
    }
    close(f);
    /* UNLINK FIRST, and only here. The agent empties cmd.bin with O_TRUNC rather than deleting it -
       sceKernelUnlink has no precedent among the plugins this console loads cleanly - so this is now
       a rename onto an EXISTING file, which is not dependable on this platform (it failed every
       single plugins.ini write until gh_ini_write_whole stopped relying on it).
       Safe here for a reason that does NOT apply to plugins.ini: the only reader of cmd.bin is our
       own agent, and a momentarily absent cmd.bin means "no request pending", which is the state it
       held a moment ago. plugins.ini is read by GoldHEN's loader when a GAME starts, so a window with
       no file there costs the owner their plugins. */
    unlink(path);
    if (rename(tmp, path) != 0) { unlink(tmp); return -1; }
    return 0;
}

static int agent_read_whole(const char *path, unsigned char *buf, size_t cap, size_t *out_n) {
    int f = open(path, O_RDONLY);
    if (f < 0) return -1;
    size_t n = 0;
    for (;;) {
        ssize_t r = read(f, buf + n, cap - n);
        if (r <= 0) break;
        n += (size_t)r;
        if (n >= cap) break;
    }
    close(f);
    *out_n = n;
    return 0;
}

/* ONE request, ONE answer. Returns 0, or negative: -1 nowhere to talk, -2 could not post the
   request, -3 no answer in time (no agent in the game), -4 malformed answer, -5 answer too big,
   and -(100+status) when the AGENT ITSELF refused - which is a different thing from not being
   there, and the panel says so. `wirelen` is the length the request is about; `inlen` is how many
   bytes actually follow it. They differ for a read, where nothing follows at all. */
/* ONE EXCHANGE AT A TIME. main() spawns a thread per connection and conn_thread takes no global
   request lock, so two agent-touching requests genuinely do run at once - and this function shares
   four things with no protection at all: the static seq counter (incremented non-atomically), the
   static req buffer, the static rsp buffer, and the single pair of cmd.bin/res.bin files on disk.
   Two callers interleaving there do not merely confuse each other's answers; one can read the other's
   reply and act on it, and acting on it means writing bytes into a running game.
   Implemented as a wrapper rather than by hand-placing unlocks: the body below has six returns and
   two continues, and six hand-placed unlocks is how a deadlock ships. */
static pthread_mutex_t g_agent_lock = PTHREAD_MUTEX_INITIALIZER;

static int agent_req_locked(int op, unsigned long long addr,
                            const void *in, unsigned int inlen, unsigned int wirelen,
                            void *out, unsigned int outcap, unsigned int *outlen) {
    if (outlen) *outlen = 0;
    /* THE WHOLE GUARD, not a third of it. wirelen was checked and inlen was not, while the memcpy
       below copies inlen bytes into a buffer sized for AGENT_CHUNK and agent_write_atomic then
       over-READS the same buffer by the same margin. inlen > wirelen is strictly stronger than
       inlen > AGENT_CHUNK here (wirelen is bounded on the line above) and it additionally rejects a
       header that declares more bytes than actually follow - which would make the agent write
       whatever happens to sit after our data into the game. */
    if (wirelen > AGENT_CHUNK || inlen > wirelen) return -5;
    if (op == AGENT_OP_WRITE && inlen != wirelen) return -5;

    static unsigned int g_seq = 0;
    unsigned int seq = ++g_seq;

    static unsigned char req[AGENT_CHUNK + AGENT_REQ_HDR];
    memcpy(req, AGENT_REQ_MAGIC, 4);
    memcpy(req + 4, &seq, 4);
    req[8] = (unsigned char)op;
    req[9] = req[10] = req[11] = 0;
    memcpy(req + 12, &addr, 8);
    memcpy(req + 20, &wirelen, 4);
    if (inlen) memcpy(req + AGENT_REQ_HDR, in, inlen);

    for (int d = 0; AGENT_DIRS[d]; d++) {
        char cmd[160], cmdt[176], res[160], alive[160];
        snprintf(cmd,  sizeof(cmd),  "%s/cmd.bin", AGENT_DIRS[d]);
        snprintf(cmdt, sizeof(cmdt), "%s/cmd.tmp", AGENT_DIRS[d]);
        snprintf(res,  sizeof(res),  "%s/res.bin", AGENT_DIRS[d]);
        snprintf(alive, sizeof(alive), "%s/alive.bin", AGENT_DIRS[d]);

        /* IS ANYONE HOME? The agent rewrites alive.bin every few seconds while it is inside a
           running game. No file, or a stale one, means there is nothing to ask - and answering
           that in one stat() is the difference between a panel that refreshes instantly and one
           that waits out the full timeout on every poll. Both sides read the same filesystem
           clock, so comparing its mtime to now needs nothing shared but the disk. */
        struct stat ast;
        if (stat(alive, &ast) != 0) continue;
        long long age = (long long)time(NULL) - (long long)ast.st_mtime;
        if (age < 0) age = 0;
        if (age > AGENT_ALIVE_MAX_AGE) continue;   /* the game it was in is gone */

        /* A stale answer from a previous round must never be read as this one's. */
        unlink(res);
        if (agent_write_atomic(cmd, cmdt, req, (size_t)AGENT_REQ_HDR + inlen) != 0) continue;

        static unsigned char rsp[AGENT_CHUNK + AGENT_RES_HDR];
        int waited = 0, stale = 0;
        while (waited < AGENT_WAIT_MS) {
            struct stat st;
            if (stat(res, &st) == 0 && st.st_size >= AGENT_RES_HDR) {
                size_t n = 0;
                if (agent_read_whole(res, rsp, sizeof(rsp), &n) == 0 && n >= AGENT_RES_HDR) {
                    /* NOTHING IS UNLINKED UNTIL THE ANSWER PARSES. The agent writes res.bin straight
                       into place now (no .tmp, no rename - see write_whole in the plugin), so this
                       can catch it mid-write. Unlinking first, which is what this used to do,
                       DELETED the agent's in-progress file: the request could then never be answered
                       and the agent's next write recreated a file nobody was waiting for. */
                    if (memcmp(rsp, AGENT_RES_MAGIC, 4) != 0) { unlink(res); return -4; }
                    unsigned int rseq = 0, rlen = 0;
                    memcpy(&rseq, rsp + 4, 4);
                    memcpy(&rlen, rsp + 12, 4);
                    if (rseq != seq) {
                        /* A LATE ANSWER IS NOT A MALFORMED ONE. The agent empties cmd.bin the moment
                           it reads it and writes res.bin only when it finishes, so after one timeout
                           its reply carries the PREVIOUS seq and lands somewhere inside the next
                           request's wait - where treating it as a protocol error charged a healthy
                           agent two failures for one slow beat. Drop that answer and keep waiting for
                           ours; bounded, because an agent that only ever answers the wrong question
                           must still end up as a timeout rather than a loop. The clock is advanced
                           explicitly - a bare continue would skip the usleep below. */
                        unlink(res);
                        if (++stale > 2) break;
                        usleep(10 * 1000);
                        waited += 10;
                        continue;
                    }
                    if (rlen > AGENT_CHUNK) { unlink(res); return -4; }
                    if ((size_t)AGENT_RES_HDR + rlen > n) {
                        /* STILL BEING WRITTEN. The header is there and it is ours, but the payload it
                           declares has not all landed - a 200 KB write spans several sceKernelWrite
                           calls. This is the ordinary case for a large read, not an error: leave the
                           file alone and look again. Measured - the PC harness turned red on exactly
                           two checks, both large payloads, when this returned -4. */
                        usleep(10 * 1000);
                        waited += 10;
                        continue;
                    }
                    int st8 = rsp[8];
                    unlink(res);
                    if (rlen) {
                        if (rlen > outcap) return -5;
                        memcpy(out, rsp + AGENT_RES_HDR, rlen);
                    }
                    if (outlen) *outlen = rlen;
                    return st8 ? -(100 + st8) : 0;
                }
            }
            /* 10 ms, NOT 50. With the agent now looking every 25 ms while a session is in
               progress, a 50 ms poll on this side would be the new floor - it would have thrown away
               half of what the other change just won. This runs only while a request is actually
               outstanding, never at rest. */
            usleep(10 * 1000);
            waited += 10;
        }
        /* No answer: take our request back so it cannot be executed later, out of order, by an
           agent that loads into the NEXT game. */
        unlink(cmd);
    }
    return -3;
}

/* The gate. TRYLOCK, not lock: /api/cheat/running and /api/mods are polled by the panel, and a
   queue on a polled route is a stall the owner reads as a hung app. Somebody else mid-exchange is a
   fact worth reporting, not worth waiting for - the next poll is half a second away. */
static int agent_req(int op, unsigned long long addr,
                     const void *in, unsigned int inlen, unsigned int wirelen,
                     void *out, unsigned int outcap, unsigned int *outlen) {
    /* A SHORT WAIT, THEN BUSY - never an open-ended queue. A bare trylock would have been wrong in
       the other direction: /api/cheat/running is polled about once a second and an exchange can take
       longer than that, so two overlapping polls would report "no helper" and the panel would gray
       the toggle out mid-session. 300 ms covers an overlap without ever holding a polled route long
       enough to read as a stall. */
    int got = 0;
    for (int t = 0; t < 6; t++) {
        if (pthread_mutex_trylock(&g_agent_lock) == 0) { got = 1; break; }
        usleep(50 * 1000);
    }
    if (!got) {
        if (outlen) *outlen = 0;
        return -6;                      /* busy: another request owns the channel right now */
    }
    int rc = agent_req_locked(op, addr, in, inlen, wirelen, out, outcap, outlen);
    pthread_mutex_unlock(&g_agent_lock);
    return rc;
}

/* THE TWO FUNCTIONS THE WHOLE CHEAT STACK IS BUILT ON. Same signatures as the PS5's, same return
   convention (0 good, negative bad), so every function above them ports across untouched. */
static int mem_read(pid_t pid, intptr_t addr, void *buf, size_t len) {
    (void)pid;                                 /* the agent IS the process - see the note above */
    unsigned char *p = (unsigned char *)buf;
    while (len) {
        unsigned int take = len > AGENT_CHUNK ? AGENT_CHUNK : (unsigned int)len;
        unsigned int got = 0;
        int rc = agent_req(AGENT_OP_READ, (unsigned long long)addr, NULL, 0, take, p, take, &got);
        if (rc != 0) return rc;
        if (got != take) return -7;
        p += take;
        addr += take;
        len -= take;
    }
    return 0;
}

static int mem_write(pid_t pid, intptr_t addr, const void *buf, size_t len) {
    (void)pid;
    const unsigned char *p = (const unsigned char *)buf;
    while (len) {
        unsigned int take = len > AGENT_CHUNK ? AGENT_CHUNK : (unsigned int)len;
        int rc = agent_req(AGENT_OP_WRITE, (unsigned long long)addr, p, take, take, NULL, 0, NULL);
        if (rc != 0) return rc;
        p += take;
        addr += take;
        len -= take;
    }
    return 0;
}

/* Is the agent in the game that is running, and what is its image base?
   Returns 0 and fills `json` with the agent's own status document, or negative when there is no
   agent to ask. The shop never invents a base: the agent reports what the module actually says. */
/* THE AGENT'S STATUS, CACHED - because every poller asks for it and they were fighting each other.
 *
 * MEASURED on a live session: seven concurrent readers, which is what an open Cheats panel plus the
 * library page produce, and 71 of 289 reads returned can_cheat=false with NO writes involved. They
 * were all queueing on the channel mutex, and a reader that cannot get it within its budget returns
 * -6, which /api/cheat/running reports as "no helper in this game" - tiles greyed, the game dropped
 * off the top of the library, the whole thing looking like a disconnect.
 *
 * A bigger timeout would only move the stall onto a polled route. The real observation is that they
 * are all asking the same question, and its answer CANNOT CHANGE while a game runs: pid, image base,
 * module name and title id are fixed from the moment the agent starts serving. So one exchange
 * answers every caller for a second.
 *
 * THE CACHE IS DELIBERATELY ONLY THE STATUS DOCUMENT. Reads and writes of game memory are never
 * cached - they are the live state a cheat is gated on, and serving a stale one would apply a cheat
 * against bytes that are no longer there.
 */
#define AGENT_INFO_TTL_MS   1000        /* one exchange serves every poller in this window */
#define AGENT_INFO_STALE_MS 8000        /* ...and a busy channel may reuse it this far back */

static pthread_mutex_t g_info_lock = PTHREAD_MUTEX_INITIALIZER;
static char            g_info_doc[512];
static long long       g_info_at;       /* now_ms() when g_info_doc was last refreshed */

static int agent_info(char *json, size_t jsz) {
    if (jsz) json[0] = 0;

    long long now = now_ms();
    pthread_mutex_lock(&g_info_lock);
    if (g_info_doc[0] && (now - g_info_at) < AGENT_INFO_TTL_MS) {
        snprintf(json, jsz, "%s", g_info_doc);
        pthread_mutex_unlock(&g_info_lock);
        return 0;
    }
    pthread_mutex_unlock(&g_info_lock);

    unsigned int n = 0;
    char tmp[512];
    int rc = agent_req(AGENT_OP_STATUS, 0, NULL, 0, 0, tmp, sizeof(tmp) - 1, &n);
    if (rc != 0) {
        /* BUSY IS NOT GONE. Another caller holds the channel; if we had a good answer moments ago it
           is still true, because none of what this document says can change while the game runs.
           Anything older than the heartbeat's own staleness window is not reused - "the agent is
           gone" has to remain reportable when it is actually true. */
        if (rc == -6) {
            pthread_mutex_lock(&g_info_lock);
            int fresh = (g_info_doc[0] && (now - g_info_at) < AGENT_INFO_STALE_MS);
            if (fresh) snprintf(json, jsz, "%s", g_info_doc);
            pthread_mutex_unlock(&g_info_lock);
            if (fresh) return 0;
        }
        return rc;
    }
    if (n >= sizeof(tmp)) n = sizeof(tmp) - 1;
    tmp[n] = 0;

    pthread_mutex_lock(&g_info_lock);
    snprintf(g_info_doc, sizeof(g_info_doc), "%s", tmp);
    g_info_at = now_ms();
    pthread_mutex_unlock(&g_info_lock);

    snprintf(json, jsz, "%s", tmp);
    return 0;
}

/* Forget it. Called where the agent may have changed under us - a new game, or the helper being
   purged - so the next question is asked for real instead of answered from a session that is over. */
static void agent_info_forget(void) {
    pthread_mutex_lock(&g_info_lock);
    g_info_doc[0] = 0;
    g_info_at = 0;
    pthread_mutex_unlock(&g_info_lock);
}

/* The game the agent is sitting in, as pid + base, in the shape the PS5's running_game() returns.
   Named the same for the same reason mem_read is: the ported cheat code calls it. */
/* DOES THE AGENT AGREE WITH THE CONSOLE ABOUT WHICH GAME THIS IS?
   1 they agree, 0 they disagree, -1 the agent reported no title of its own.

   Written once because it has to hold at TWO places and only held at one. The pid and base describe
   the process the agent is sitting in; the title comes from the console's foreground app. They are
   normally the same game and can come apart - the owner closes one game and opens another, and for a
   moment an agent still answering from the old process hands out a live pid and base belonging to a
   title that is no longer running. Writing a cheat then writes it into the wrong game, which is the
   exact class of fault this project has already shipped once (a PS4 game's toggles reaching the PS5).
   Refusing is the right answer rather than preferring one source: whichever is stale, we cannot tell
   which, and "the game is not ready" is a true thing to say until they agree again. */
static int agent_title_agrees(const char *info, const char *console_tid,
                              char *seen_out, size_t seen_sz) {
    if (seen_sz) seen_out[0] = 0;
    const char *at = info ? strstr(info, "\"title_id\":\"") : NULL;
    if (!at) return -1;
    char seen[16] = {0};
    const char *q = at + 12;
    size_t k = 0;
    while (q[k] && q[k] != '"' && k < sizeof(seen) - 1) { seen[k] = q[k]; k++; }
    if (!seen[0]) return -1;
    if (seen_sz) snprintf(seen_out, seen_sz, "%s", seen);
    return (console_tid && !strcmp(seen, console_tid)) ? 1 : 0;
}

/* THE IMAGE BASE IS A FACT SOMEBODY CAN BE ASKED FOR, not a default to fall back on first. Two
   routes used to reach straight for the PS4's no-ASLR load address whenever the caller left `base`
   out, so a game that did NOT load there had every cheat offset computed from the wrong place - and
   nothing downstream could tell a guessed base from a measured one. The agent measures it for
   itself and reports it, so the order is: what the caller said, then what the agent says, and only
   then the no-ASLR address - the same order ps4_patch_action_json already uses. */
static intptr_t agent_image_base(void) {
    char info[512];
    if (agent_info(info, sizeof(info)) != 0) return 0;
    const char *b = strstr(info, "\"base\":\"0x");
    if (!b) return 0;
    return (intptr_t)strtoull(b + 10, NULL, 16);
}

/* The module's size, straight out of the same reply agent_image_base() reads. Needed so "search the
   whole module" is a measured span rather than a guessed one. 0 when the agent cannot say. */
static long long agent_image_size(void) {
    char info[512];
    if (agent_info(info, sizeof(info)) != 0) return 0;
    const char *z = strstr(info, "\"size\":");
    if (!z) return 0;
    return strtoll(z + 7, NULL, 10);
}

static int running_game(char *title, size_t tsz, pid_t *out_pid, intptr_t *out_base) {
    if (tsz) title[0] = 0;
    if (out_pid) *out_pid = 0;
    if (out_base) *out_base = 0;

    char info[512];
    if (agent_info(info, sizeof(info)) != 0) return -1;      /* no agent -> no live game for us */

    const char *b = strstr(info, "\"base\":\"0x");
    const char *p = strstr(info, "\"pid\":");
    if (!b || !p) return -1;
    if (out_base) *out_base = (intptr_t)strtoull(b + 10, NULL, 16);
    if (out_pid)  *out_pid  = (pid_t)strtol(p + 6, NULL, 10);

    /* The TITLE comes from the console, not from the agent: the agent knows which module it is in,
       and app.db plus sceKernelGetAppInfo know which game that is. One fact, one source. */
    running_title_id(title, tsz);
    if (!title[0]) return -1;

    /* AND THE TWO SOURCES MUST AGREE - see agent_title_agrees above for why this is a refusal. */
    char seen[16] = {0};
    if (agent_title_agrees(info, title, seen, sizeof(seen)) == 0) {
        ilog("running_game: the agent is in %s but the console says %s - refusing", seen, title);
        if (out_pid) *out_pid = 0;
        if (out_base) *out_base = 0;
        if (tsz) title[0] = 0;
        return -1;
    }
    return 0;
}

/* THE CHEAT ENGINE ITSELF - the PS5's, copied by tools/ps4_sync_cheat_core.py and checked by both
   builds. It is included HERE and not at the top of the file because everything in it is written
   against mem_read, mem_write and running_game, which are defined immediately above. */
#include "cheat_core.h"

/* ---------------- THE ARCHIVE AND THE ART, both of which needed a decompressor ---------------
 *
 * THE SAME FILES THE PS5 BUILD INCLUDES. Not a generated copy like github_lane.h below: these
 * were written after both builds existed, so neither had to be left alone, and a copy is a thing
 * that rots. Everything they need from a host - mkparents, ilog, json_str_after, SHOP_DATA_DIR -
 * this file already had.
 *
 * tools/test_archive.py and tools/test_thumb.py are the only reason to trust any of it. See the
 * same block in ps5-app/onconsole/server.c for what each file does.
 * -------------------------------------------------------------------------------------------- */
#define THUMB_CACHE_DIR  SHOP_DATA_DIR "/thumbs"

/* json_str_after, which the shared planner reads param.json with, is already here: cheat_core.h
   defines it. Writing a second one next to it was a redefinition the compiler caught at once -
   which is the right outcome, and the reason these headers name what they need from a host. */

#include "../../shared/pms_inflate.h"
#include "../../shared/pms_zip.h"
#include "../../shared/pms_zplan.h"
#include "../../shared/pms_install.h"
#include "../../shared/pms_thumb.h"

/* ---- the console's own cover art -----------------------------------------------------------
 *
 * WHY THIS CONSOLE ESPECIALLY. /thumb/<TID>.jpg falls back to the title's full-size icon0.png
 * when there is no small copy, and on this console there never was one - the cache was filled
 * only by a companion. Measured on the owner's PS4: 23 covers, 7.1 MB, mean 325 KB each, and this
 * is the browser that least tolerates it. Through pms_thumb.h the same covers are under 1 MB.
 */
static volatile int g_thumb_busy;

static int thumb_fill_one(const char *tid) {
    char src[700], dst[700];
    struct stat st;
    if (!tid || !tid[0]) return -1;
    snprintf(dst, sizeof(dst), "%s/%s.jpg", THUMB_CACHE_DIR, tid);
    /* ALREADY THERE IS ALREADY DONE, whoever made it. A companion that filled this cache is not
       second-guessed; this only fills the gaps. */
    if (stat(dst, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0) return 1;
    if (!icon_path_for(tid, src, sizeof(src))) return -1;
    int rc = thumb_make(src, dst, THUMB_PX, THUMB_QUALITY);
    if (rc != THUMB_OK) {
        ilog("thumb: %s could not be made (%d)", tid, rc);
        return -1;
    }
    return 0;
}

static void *thumb_worker(void *arg) {
    (void)arg;
    mkparents(THUMB_CACHE_DIR "/x");
    mkdir(THUMB_CACHE_DIR, 0777);
    int made = 0, skipped = 0, failed = 0;
    DIR *d = opendir(APPMETA_ROOT);
    if (d) {
        struct dirent *e;
        while ((e = readdir(d))) {
            if (e->d_name[0] == '.') continue;
            int rc = thumb_fill_one(e->d_name);
            if (rc == 0) made++;
            else if (rc == 1) skipped++;
            else failed++;
        }
        closedir(d);
    }
    if (made || failed)
        ilog("thumb: %d made, %d already there, %d could not be made", made, skipped, failed);
    g_thumb_busy = 0;
    return NULL;
}

/* ON A THREAD, even though this server is thread-per-connection: one pass decodes every PNG in
   the library, and doing that on the connection that asked for the first cover would make that
   one request pay for all of them. */
static void thumb_fill_start(void) {
    if (g_thumb_busy) return;
    g_thumb_busy = 1;
    pthread_t t;
    pthread_attr_t a;
    pthread_attr_init(&a);
    pthread_attr_setstacksize(&a, 512 * 1024);
    if (pthread_create(&t, &a, thumb_worker, NULL) != 0) g_thumb_busy = 0;
    else pthread_detach(t);
    pthread_attr_destroy(&a);
}

/* ---------------- downloading from a project, with no PC on the network ----------------------
 * The shared half of this - TLS, sha256, reading a release document, the six-hour cache - is
 * github_lane.h, generated from ps5-app/onconsole/server.c by tools/ps4_sync_github_lane.py and
 * checked for drift by both ELF builds. What is below is the PS4's own half.
 *
 * ITS OWN JOB RECORD. g_job in this file is the install/spawn job: it has a task, a title id and
 * a free-form state string, and the companion reads that shape. A download is a different thing
 * and gets a different record, rather than changing a contract that works.
 * -------------------------------------------------------------------------------------------- */
#define DL_BUF (1 << 20)

typedef struct {
    int       id;
    int       active, cancel;
    long long done, total;
    char      state[16];              /* downloading | done | error | idle */
    char      name[200];
    char      msg[300];
    char      path[700], final[700];  /* <final>.part while it is arriving, then <final> */
    char      digest[72];
    char      purpose[16];
    int       stem_clean;
    /* The release this file came from, written beside it when the transfer finishes so the panel
       can stop offering an update that has already been taken. */
    char      tag[120];
    /* THE ARCHIVE HALF, exactly as the PS5's record carries it - the page sends one request shape
       to either console. See the note on dl_job_t in ps5-app/onconsole/server.c. */
    int       archive;
    char      ar_root[400];
    char      ar_tid[32];
    char      ar_data[400];
} p4_dlj_t;

static p4_dlj_t        g_dlj;
static void dlj_progress_files(long long done, long long total);  /* with the other hooks below */
static int             g_dlj_seq;
static pthread_mutex_t g_dlj_lock = PTHREAD_MUTEX_INITIALIZER;

static void dlj_say(const char *state, const char *msg) {
    pthread_mutex_lock(&g_dlj_lock);
    if (state) snprintf(g_dlj.state, sizeof(g_dlj.state), "%s", state);
    if (msg)   snprintf(g_dlj.msg, sizeof(g_dlj.msg), "%s", msg);
    pthread_mutex_unlock(&g_dlj_lock);
}

/* The three hooks github_lane.h reports through. Each console implements them over whatever it
   tracks a transfer with; that is why the shared code does not carry a job struct. */
static void pms_dl_progress(long long done, long long total) {
    pthread_mutex_lock(&g_dlj_lock);
    g_dlj.done = done;
    if (total >= 0) g_dlj.total = total;
    pthread_mutex_unlock(&g_dlj_lock);
}

/* The same two numbers, for the UNPACKING half of a job. Files rather than bytes: the compressed
   total is known before a byte is written and the uncompressed one is not, so counting entries is
   the honest fraction and the bar reaches the end exactly once. */
static void dlj_progress_files(long long done, long long total) {
    pthread_mutex_lock(&g_dlj_lock);
    g_dlj.done = done;
    g_dlj.total = total;
    pthread_mutex_unlock(&g_dlj_lock);
}

static int pms_dl_cancelled(void) {
    pthread_mutex_lock(&g_dlj_lock);
    int c = g_dlj.cancel;
    pthread_mutex_unlock(&g_dlj_lock);
    return c;
}

static long long pms_dl_expected(void) {
    pthread_mutex_lock(&g_dlj_lock);
    long long t = g_dlj.total;
    pthread_mutex_unlock(&g_dlj_lock);
    return t;
}

/* The two questions the shared same-stem cleanup asks this console. */
static void pms_file_stem(const char *name, char *out, size_t outsz) {
    p4_stem(name, out, outsz);
}

static int pms_name_is_bundled(const char *name) {
    for (int i = 0; i < PS4_PAYLOAD_BUNDLE_COUNT; i++)
        if (!strcmp(PS4_PAYLOAD_BUNDLE[i].filename, name)) return 1;
    return 0;
}

#include "github_lane.h"

static void *p4_dlj_thread(void *arg) {
    (void)arg;
    /* `purpose` is accepted by the route and recorded, so the page sends one request shape to
       either console - but this console has no backup service to name, so there is only one
       sentence to say and the thread does not read it. */
    char url[1300], part[700], final[700], want[72], name[200], rtag[120];
    int clean;
    char err[260] = {0};
    pthread_mutex_lock(&g_dlj_lock);
    snprintf(url, sizeof(url), "%s", g_dlj.path);        /* path carried the url in; see the route */
    snprintf(part, sizeof(part), "%s.part", g_dlj.final);
    snprintf(final, sizeof(final), "%s", g_dlj.final);
    snprintf(want, sizeof(want), "%s", g_dlj.digest);
    snprintf(name, sizeof(name), "%s", g_dlj.name);
    snprintf(rtag, sizeof(rtag), "%s", g_dlj.tag);
    clean = g_dlj.stem_clean;
    int ar_want = g_dlj.archive;
    char ar_root[400], ar_tid[32], ar_data[400], ar_tag[120];
    snprintf(ar_tag, sizeof(ar_tag), "%s", g_dlj.tag);
    snprintf(ar_root, sizeof(ar_root), "%s", g_dlj.ar_root);
    snprintf(ar_tid, sizeof(ar_tid), "%s", g_dlj.ar_tid);
    snprintf(ar_data, sizeof(ar_data), "%s", g_dlj.ar_data);
    snprintf(g_dlj.path, sizeof(g_dlj.path), "%s", part);
    pthread_mutex_unlock(&g_dlj_lock);

    dlj_say("downloading", "Downloading");
    int rc = https_download(url, part, err, sizeof(err));
    if (rc != 0) {
        char m[340];
        snprintf(m, sizeof(m), "The download did not finish (%s).",
                 err[0] ? err : (rc == -2 ? "canceled" : "unknown"));
        unlink(part);
        dlj_say(rc == -2 ? "idle" : "error", m);
        goto finish;
    }
    /* IS IT THE FILE WE WERE PROMISED? Checked while it is still the .part, so a file that is not
       what the release described never reaches the name something would run. */
    if (want[0]) {
        char got[72] = {0};
        dlj_say("downloading", "Checking the download");
        if (sha256_file(part, got, sizeof(got)) != 0 || strcasecmp(got, want)) {
            ilog("fetch: %s does not match the release's sha256", final);
            unlink(part);
            dlj_say("error", "The download does not match what the project published - it was not "
                            "kept.");
            goto finish;
        }
    }
    if (clean) dl_drop_same_stem(final);
    unlink(final);                     /* rename-onto-existing does not replace on this console */
    if (rename(part, final) != 0) {
        dlj_say("error", "Downloaded, but the file could not be put in place.");
        goto finish;
    }
    pthread_mutex_lock(&g_dlj_lock);
    snprintf(g_dlj.path, sizeof(g_dlj.path), "%s", final);
    pthread_mutex_unlock(&g_dlj_lock);
    {   /* WRITE DOWN WHAT WAS TAKEN, now that the file is in place - see dl_take_note. */
        char ndir[700], nbase[300];
        snprintf(ndir, sizeof(ndir), "%s", final);
        char *nsl = strrchr(ndir, '/');
        if (nsl) {
            *nsl = 0;
            snprintf(nbase, sizeof(nbase), "%s", nsl + 1);
            dl_take_note(ndir, nbase, rtag);
        }
    }
    ilog("fetch: delivered %s", final);
    /* AND IF IT IS AN ARCHIVE, THE JOB IS NOT OVER. Opening it is the rest of the same job, so
       the owner watches one progress bar and ends up with an installed emulator. It writes its
       own state and its own sentence either way. */
    if (ar_want) {
        static arinst_t r;
        dlj_say("installing", "Unpacking");
        int arc = archive_install(final, ar_root, ar_tid, ar_data, &r, dlj_progress_files);
        if (arc != 0) {
            dlj_say("error", r.why[0] ? r.why
                                      : "That archive could not be opened on this console.");
            ilog("archive: %s failed (%d) %s", final, arc, r.why);
            goto finish;
        }
        /* WRITE DOWN WHICH RELEASE THIS APP FOLDER NOW IS, before the archive goes - beside the
           FOLDER, not beside the archive, which is deleted on the next line and would take the
           note with it. See the same block in ps5-app/onconsole/server.c for the measurement
           that produced this. */
        if (r.placed[0] && r.tid[0] && ar_tag[0]) dl_take_note(ar_root, r.tid, ar_tag);
        /* THE ARCHIVE GOES: it is the biggest file on the console at this point and everything
           inside it that matters is now on disk under its own name. */
        unlink(final);
        pthread_mutex_lock(&g_dlj_lock);
        snprintf(g_dlj.path, sizeof(g_dlj.path), "%s", r.pkg[0] ? r.pkg : r.placed);
        pthread_mutex_unlock(&g_dlj_lock);
        dlj_say("done", r.pkg[0] ? "Unpacked - the package is ready to install"
                                 : (r.why[0] ? r.why : "Installed"));
        ilog("archive: %s -> %s (%lld files, %lld MB)", final,
             r.pkg[0] ? r.pkg : r.placed, r.files, r.bytes >> 20);
        notifyf("%s is on your console", r.tid[0] ? r.tid : (name[0] ? name : "Your download"));
        goto finish;
    }
    dlj_say("done", "Downloaded");
    notifyf("%s is on your console", name[0] ? name : "Your download");
finish:
    pthread_mutex_lock(&g_dlj_lock);
    g_dlj.active = 0;
    pthread_mutex_unlock(&g_dlj_lock);
    return NULL;
}


/* Defined further down, beside the cache it guards. Declared here because the cheat routes need a
   title's installed version and they are wired in above it. */
static int console_titles_cached(ps4_title_t *out, int max);

/* The installed version of a title, from the console's own app.db. cheat_pick_file() needs it to
   choose between several files for one game, and an empty answer is fine - it then picks the best
   it has and says so in `reason`. */
static void installed_ver_for(const char *tid, char *out, size_t outsz) {
    if (outsz) out[0] = 0;
    ps4_title_t *rows = (ps4_title_t *)calloc(MAX_TITLES, sizeof(ps4_title_t));
    if (!rows) return;
    int n = console_titles_cached(rows, MAX_TITLES);
    for (int i = 0; i < n; i++)
        if (!strcmp(rows[i].tid, tid)) { snprintf(out, outsz, "%s", rows[i].ver); break; }
    free(rows);
}

/* Does the library hold anything at all for this title - a cheat file for any version, or a game
   patch? Either one needs the helper, because both are written into the running game. Defined here
   rather than beside the rest of autoarm because it needs installed_ver_for, cheat_pick_file and
   patch_file_for, none of which exist that early in this file. */
static int title_wants_agent(const char *tid) {
    char iver[48] = {0};
    installed_ver_for(tid, iver, sizeof(iver));
    char pick[600]; const char *why = "none";
    (void)cheat_pick_file(tid, iver, pick, sizeof(pick), &why);
    if (pick[0]) return 1;
    char pf[600] = {0};
    return patch_file_for(tid, pf, sizeof(pf)) == 0;
}

/* DECIDE AND APPLY: the helper is listed for exactly those installed titles the library has something
 * for, and for no others. Cheap and exact - both halves are facts we already hold.
 *
 * Measured on this console: 14 installed titles, 5 with cheats. So this writes five small [TID]
 * sections and a game with nothing never loads our module at all, which is strictly better than the
 * blanket [default] form.
 *
 * Returns the number of titles armed, or -1 if the list could not be written. Writes nothing when the
 * result would be identical to what is already there, so this is safe to call on every boot and after
 * every library change.
 */
static int agent_autoarm_reconcile(void) {
    if (!autoarm_enabled()) return 0;
    autoarm_seed_skip();

    ps4_title_t *rows = (ps4_title_t *)calloc(MAX_TITLES, sizeof(ps4_title_t));
    if (!rows) return -1;
    int n = console_titles_cached(rows, MAX_TITLES);

    /* A LOCAL, NOT A STATIC. 64 x 16 is one kilobyte, and a function-local static is shared by
       every thread in the function - the watcher and /api/cheat/rescan both reach this. */
    char wanted[AUTOARM_MAX][16];
    int nw = 0;
    for (int i = 0; i < n && nw < AUTOARM_MAX; i++) {
        const char *tid = rows[i].tid;
        if (!tid[0]) continue;
        /* A title id and nothing else can become a section header. */
        int ok = 1;
        for (const char *c = tid; *c; c++)
            if (!((*c >= 'A' && *c <= 'Z') || (*c >= 'a' && *c <= 'z') ||
                  (*c >= '0' && *c <= '9'))) { ok = 0; break; }
        if (!ok) continue;
        if (autoarm_skipped(tid)) continue;
        if (!title_wants_agent(tid)) continue;
        int dup = 0;
        for (int k = 0; k < nw; k++) if (!strcmp(wanted[k], tid)) { dup = 1; break; }
        if (!dup) snprintf(wanted[nw++], sizeof(wanted[0]), "%s", tid);
    }
    free(rows);

    /* The module has to be on the console before it is listed - listing a file that is not there is
       harmless but pointless, and this is also what refreshes it after the shop is rebuilt. */
    if (nw && agent_deploy() != 0) return -1;

    int armed = 0;
    if (agent_autoarm_apply((const char (*)[16])wanted, nw, &armed) != 0) return -1;
    /* Our own message replaces GoldHEN's gold banner whenever anything of ours is listed. */
    (void)gh_set_load_notification(armed ? 0 : 1);
    return armed;
}

/* WHICH FILE DOES THIS REQUEST MEAN? Without a version, the installed one - which is what
   mods_file_for has always answered. With a version, THAT version's file, and if the library has no
   file for exactly that version the request is REFUSED rather than quietly served from another one.
   The refusal is the whole point: a mod is applied by its INDEX into the file the panel listed, so
   serving a different file under the same index applies a different cheat and reports success under
   the wrong name. Returns 0 resolved, -1 nothing at all, -2 nothing for that exact version. */
static int mods_file_for(const char *tid, char *out, size_t outsz);

static int mods_file_for_req(const char *tid, const char *want_ver, char *out, size_t outsz) {
    if (outsz) out[0] = 0;
    if (!want_ver || !want_ver[0]) return mods_file_for(tid, out, outsz);
    const char *why = "none";
    int exact = cheat_pick_file(tid, want_ver, out, outsz, &why);
    if (!out[0]) return -1;
    if (!exact) { if (outsz) out[0] = 0; return -2; }
    return 0;
}

/* Which cheat file would we use for this title? Same two steps as the PS5's mods_file_for(). */
static int mods_file_for(const char *tid, char *out, size_t outsz) {
    char iver[48] = {0};
    installed_ver_for(tid, iver, sizeof(iver));
    const char *why = "none";
    cheat_pick_file(tid, iver, out, outsz, &why);
    return out[0] ? 0 : -1;
}

/* ---- WHY THERE IS NO "START A GAME" HERE ------------------------------------------------------
 * It was written and then taken out again, because it cannot be done from this payload on this
 * firmware. Measured with /api/engine/symprobe against all five libraries this payload loads:
 *
 *     sceLncUtilLaunchApp              false      <- the only one with a real signature
 *     sceLncUtilGetAppStatus           false
 *     sceSystemServiceLaunchApp        true       <- declared `void f()`: no arguments, no shape
 *     sceSystemServiceKillApp          true       <- typed, but three of its four ints are unnamed
 *     sceUserServiceGetForegroundUser  true
 *     sceSystemServiceGetAppIdOfBigApp true
 *
 * The toolchain declares sceLncUtilLaunchApp in full - (const char *title_id, const char *argv[],
 * LncAppParam *param) - and there is no libSceLncUtil.sprx to resolve it from: /system/common/lib
 * has 439 entries and /system/priv/lib has 23, both listed from the console, and it is in neither.
 * Its exports live inside ShellCore.
 *
 * sceSystemServiceLaunchApp resolves, and using it would mean inventing its arguments - the same
 * shape as sceSystemServiceLaunchWebBrowser, which ps4-app/tile-pkg/pms/main.c already carries a
 * comment about. This project crashed a console once by inferring a call it had not read, and the
 * rule that came out of that has no exceptions. sceSystemServiceKillApp fails the same test from
 * the other side: typed, but three of its four integers have no documented meaning, so there would
 * be no honest way to close what we had opened.
 *
 * The consequence is worth stating plainly rather than leaving implicit: the last step of the PS4
 * cheat chain - a cheat actually landing in a running game - is verified by somebody starting a
 * game, and nothing here can stand in for that.
 */

/* The PS5's patch_action_json, written against this payload's row type. Everything it actually
   does lives in patch_apply/patch_revert, which ARE shared; this only gathers the answer. */
static void ps4_patch_action_json(const char *tid, int index, int force, int dry, int is_revert,
                                  char *out, size_t outsz) {
    char iver[48] = {0};
    installed_ver_for(tid, iver, sizeof(iver));

    /* DOES THIS TITLE HAVE A PATCH FILE AT ALL - asked FIRST. Asking "is the game running" first
       told an owner to launch a game in order to be refused for a completely different reason,
       which is a wrong answer dressed as a helpful one. */
    char pfile[600] = {0};
    if (patch_file_for(tid, pfile, sizeof(pfile)) != 0) {
        snprintf(out, outsz,
                 "{\"ok\":false,\"error\":\"no_patch_file\",\"title_id\":\"%s\","
                 "\"message\":\"There is no game patch file for this title on the console.\"}",
                 tid);
        return;
    }

    /* THE VERSION GATE, which the PS5 has had from the start and this did not. A game patch is a
       list of byte offsets into one exact build of a game; applied to another build those offsets
       point at unrelated code. The expect-gate below refuses most of it, but "most" is not a thing
       to rely on inside a running game. force=1 is still honoured - that is what it is for. */
    char aver[32] = {0}, pname[160] = {0};
    patch_meta_attr(tid, index, "AppVer", aver, sizeof(aver));
    patch_meta_attr(tid, index, "Name", pname, sizeof(pname));
    if (!is_revert && !force && aver[0] && iver[0] && strcmp(aver, iver) != 0) {
        char ea[64], eb[64];
        json_escape(aver, ea, sizeof(ea));
        json_escape(iver, eb, sizeof(eb));
        snprintf(out, outsz,
                 "{\"ok\":false,\"error\":\"version_mismatch\",\"title_id\":\"%s\","
                 "\"app_ver\":\"%s\",\"installed_version\":\"%s\",\"message\":\"This patch "
                 "was written for version %s and version %s is installed.\"}",
                 tid, ea, eb, ea, eb);
        return;
    }

    char rtid[24] = {0};
    pid_t pid = 0; intptr_t base = 0;
    int live = (running_game(rtid, sizeof(rtid), &pid, &base) == 0 && !strcmp(rtid, tid));
    if (!live) {
        snprintf(out, outsz,
                 "{\"ok\":false,\"error\":\"game_not_running\",\"title_id\":\"%s\","
                 "\"message\":\"Launch the game first - a patch is written into its live "
                 "memory.\"}", tid);
        return;
    }
    /* 0x400000 is the PS4's no-ASLR load address and what every offset in the library is relative
       to. The agent reports the real one and that is what is used; this only covers an agent that
       answered without a base, where guessing nothing would mean writing to offset-from-zero. */
    if (!base) base = (intptr_t)PATCH_NO_ASLR;

    char detail[400] = {0};
    int rc = is_revert ? patch_revert(tid, index, iver, pid, base, detail, sizeof(detail))
                       : patch_apply(tid, index, iver, pid, base, force, dry, detail, sizeof(detail));
    if (!dry) patch_result_toast(pname[0] ? pname : "Game patch", is_revert, rc, detail, tid);

    /* The same keys the PS5 answers with, because the panel reads them by name and a missing key
       is a blank line in the UI, not an error anybody can see. */
    int partial = 0, written = 0, failed = 0;
    {
        const char *w = strstr(detail, "written=");
        const char *f = strstr(detail, "failed=");
        if (w) written = atoi(w + 8);
        if (f) failed = atoi(f + 7);
        partial = (written > 0 && failed > 0);
    }
    char ed[800], en[400], ea2[64], eb2[64];
    json_escape(detail, ed, sizeof(ed));
    json_escape(pname, en, sizeof(en));
    json_escape(aver, ea2, sizeof(ea2));
    json_escape(iver, eb2, sizeof(eb2));
    const char *msg = rc >= 0
        ? (partial ? "The patch was only partly applied." : (is_revert ? "Patch reverted."
                                                                       : "Patch applied."))
        : "The console refused this patch.";
    snprintf(out, outsz,
             "{\"ok\":%s,\"rc\":%d,\"index\":%d,\"title_id\":\"%s\",\"name\":\"%s\","
             "\"app_ver\":\"%s\",\"installed_version\":\"%s\",\"revert\":%s,\"dry\":%s,"
             "\"partial\":%s,\"pid\":%d,\"base\":\"0x%llx\",\"detail\":\"%s\","
             "\"message\":\"%s\"}",
             rc >= 0 ? "true" : "false", rc, index, tid, en, ea2, eb2,
             is_revert ? "true" : "false", dry ? "true" : "false",
             partial ? "true" : "false",
             (int)pid, (unsigned long long)base, ed, msg);
}




static void bgft_bootstrap(void) {
    static const char *LIBS[] = {
        "/system/common/lib/libSceBgft.sprx",
        "/system/common/lib/libSceAppInstUtil.sprx",
        "/system/common/lib/libSceUserService.sprx",
        "/system/common/lib/libSceSystemService.sprx",
        /* MEASURED BY /api/engine/proclist, which came back have_getappinfo:false on a console
           that plainly has the call. sceKernelGetAppInfo lives in libkernel, which was never in
           this list - and RTLD_DEFAULT does not reach it, exactly as it did not reach a single
           BGFT symbol when this file was first written. Same lesson, second library. */
        "/system/common/lib/libkernel.sprx",
        NULL
    };
    for (int i = 0; LIBS[i]; i++) {
        int mres = 0;
        sceKernelLoadStartModule(LIBS[i], 0, 0, 0, 0, &mres);
        void *h = dlopen(LIBS[i], RTLD_LAZY | RTLD_GLOBAL);
        if (h && g_dl_n < DL_MAX) g_dl[g_dl_n++] = h;
    }
    bgft_init_fn     = (pfn_bgft_init_t)      dlsym_any("sceBgftServiceIntInit");
    bgft_register_fn = (pfn_bgft_register_t)  dlsym_any("sceBgftServiceIntDebugDownloadRegisterPkg");
    bgft_start_fn    = (pfn_bgft_start_t)     dlsym_any("sceBgftServiceIntDownloadStartTask");
    bgft_stop_fn     = (pfn_bgft_stop_t)      dlsym_any("sceBgftServiceIntDownloadStopTask");
    bgft_unreg_fn    = (pfn_bgft_unregister_t)dlsym_any("sceBgftServiceIntDownloadUnregisterTask");
    bgft_progress_fn = (pfn_bgft_progress_t)  dlsym_any("sceBgftServiceIntDownloadGetProgress");
    ai_tid_from_pkg_fn   = (pfn_ai_tid_from_pkg_t)  dlsym_any("sceAppInstUtilGetTitleIdFromPkg");
    ai_exists_fn         = (pfn_ai_exists_t)        dlsym_any("sceAppInstUtilAppExists");
    ai_prep_overwrite_fn = (pfn_ai_prep_overwrite_t)dlsym_any("sceAppInstUtilAppPrepareOverwritePkg");
    ai_install_pkg_fn    = (pfn_ai_install_pkg_t)   dlsym_any("sceAppInstUtilAppInstallPkg");
    ai_progress_info_fn  = (pfn_ai_progress_info_t) dlsym_any("sceAppInstUtilGetInstallProgressInfo");
    ai_is_installing_fn  = (pfn_ai_is_installing_t) dlsym_any("sceAppInstUtilAppIsInInstalling");

    sceUserServiceInitialize(NULL);
    int air = sceAppInstUtilInitialize();

    if (!bgft_init_fn || !bgft_register_fn || !bgft_start_fn) {
        ilog("install: BGFT is incomplete on this firmware (init=%p reg=%p start=%p) - installs refused",
             (void *)bgft_init_fn, (void *)bgft_register_fn, (void *)bgft_start_fn);
        return;
    }
    g_bgft_heap = malloc(BGFT_HEAP_SIZE);
    if (!g_bgft_heap) { ilog("install: no memory for the BGFT heap"); return; }
    memset(g_bgft_heap, 0, BGFT_HEAP_SIZE);
    OrbisBgftInitParams p;
    memset(&p, 0, sizeof(p));
    p.heap = g_bgft_heap;
    p.heapSize = BGFT_HEAP_SIZE;
    int rc = bgft_init_fn(&p);
    /* A NON-ZERO INIT IS NORMAL ON A RELOAD. GoldHEN injects every payload into the SAME host
       process, so when this build replaces a previous one the service is already initialised and
       init answers 0x80990001 - measured, first load rc=0 and the very next load rc=0x80990001.
       Refusing installs on that would mean the shop only ever worked until its first update.
       So we stay ready and record the code; if the service really were unusable, the register call
       below is where it shows up, with a message that says so. */
    g_bgft_ready = 1;
    if (rc == 0)
        ilog("install: BGFT ready (init rc=0, AppInstUtil rc=0x%08X)", (unsigned)air);
    else {
        /* GIVE THE HEAP BACK WHEN THE SERVICE DID NOT TAKE IT.
           A non-zero init means BGFT was already initialised by an earlier load of this payload,
           and it is still using THAT load's heap - so the megabyte just allocated here is dead
           weight that is never freed, in a SHARED system daemon, once per reload.
           Measured after roughly fifteen reloads in one session: /api/library began answering
           "out of memory" because a 256 KB allocation for the library JSON could no longer be
           satisfied, and a BGFT register failed with 0x8099002C. The payload image and its threads
           from each reload cannot be reclaimed from inside - a console restart is the only cure for
           those - but this part is ours to not waste. */
        free(g_bgft_heap);
        g_bgft_heap = NULL;
        ilog("install: BGFT already initialised in this process (init rc=0x%08X, AppInstUtil rc=0x%08X) "
             "- continuing, and the spare heap was given back", (unsigned)rc, (unsigned)air);
    }
}

/* A PS4 PKG's content id sits at 0x40 in the header. We only need it to name the BGFT task. */
static int pkg_content_id_from_url_name(const char *name, char *out, size_t outsz) {
    /* The companion puts the content id in the file name when it knows it; otherwise the title id
       is enough for BGFT to accept the task. Pull a CUSAxxxxx out of whatever we were given. */
    const char *p = name;
    while (*p) {
        if ((p[0] == 'C' && p[1] == 'U' && p[2] == 'S' && p[3] == 'A') ||
            (p[0] == 'N' && p[1] == 'P' && p[2] == 'X' && p[3] == 'S')) {
            int digits = 1;
            for (int i = 4; i < 9; i++) if (p[i] < '0' || p[i] > '9') { digits = 0; break; }
            if (digits) { snprintf(out, outsz, "%.9s", p); return 1; }
        }
        p++;
    }
    out[0] = 0;
    return 0;
}

/* ------------------------------------------------- not leaving tasks behind
 *
 * BGFT keeps one directory per registered task under /user/bgft/task and that table is NOT
 * unlimited. After a handful of test installs on this console every new register started coming
 * back 0x80990086 while the directories of those dead tasks were still sitting there - which was
 * read as a full table and is not (see bgft_sweep_ours): a task only
 * disappears when somebody unregisters it, so a shop that does not clean up after itself ends up
 * unable to install anything at all - including the packages it installed fine an hour earlier.
 *
 * Two halves, because one is not enough on its own:
 *   - going forward, a job that reaches a terminal state releases its own task (bgft_release);
 *   - for anything already stranded, we sweep. A payload reload loses the ids we were holding, and
 *     an install interrupted by a crash or a power cut leaves a task nobody remembers, so the ids
 *     have to be recovered from the console rather than from memory.
 *
 * WHICH TASKS ARE OURS. BGFT writes the task record as plain text inside d0.pdb, download url and
 * all. Every install we start is plain http on one of two routes we own - /library/ on a companion,
 * or /pkgfile/ on this console itself - and a real PlayStation Store task is https on a Sony host.
 * Both halves must match before we touch anything, so the tasks this PS4 already had from the
 * user's own store downloads are left exactly as they are. Requiring http alone would not be
 * enough: this console's own firmware-update task is plain http too, and carries neither route.
 *
 * BOTH ROUTES, NOT JUST /library/. The first version of this matched only the companion route, so a
 * task left behind by an install from the console's own storage was invisible to the sweep and sat
 * in the table forever - the exact leak this function exists to clear.
 *
 * We never sweep the job we are currently following either; the install route refuses to start a
 * second job while one is live, so at sweep time ours is the only one that can be running.
 */
#define BGFT_TASK_ROOT "/user/bgft/task"

static int bgft_task_is_ours(const char *dir) {
    char p[320];
    snprintf(p, sizeof(p), "%s/%s/d0.pdb", BGFT_TASK_ROOT, dir);
    long n = 0;
    char *b = slurp(p, &n);
    if (!b) return 0;
    /* The record is binary with NUL-terminated strings in it, so this walks the bytes rather than
       treating the buffer as one string - a strstr would stop at the first NUL. */
    int ours = 0;
    for (long i = 0; i + 7 <= n && !ours; i++) {
        if (memcmp(b + i, "http://", 7)) continue;
        for (long j = i + 7; j < n && b[j]; j++) {
            if (j + 9 <= n && !memcmp(b + j, "/library/", 9)) { ours = 1; break; }
            if (j + 9 <= n && !memcmp(b + j, "/pkgfile/", 9)) { ours = 1; break; }
        }
    }
    free(b);
    return ours;
}

/* The title id a task names, read out of the content id inside its own record. "" if not found.

   A content id is fixed-shape - six characters, '-', a nine-character title id, '_', two digits,
   '-' - so it can be recognised in a binary record by its punctuation without knowing the format.
   This walks bytes rather than using strstr, for the same reason bgft_task_is_ours does: the
   record is full of NULs. */
static void bgft_task_title(const char *dir, char *out, size_t outsz) {
    out[0] = 0;
    char p[320];
    snprintf(p, sizeof(p), "%s/%s/d0.pdb", BGFT_TASK_ROOT, dir);
    long n = 0;
    char *b = slurp(p, &n);
    if (!b) return;
    for (long i = 0; i + 20 <= n; i++) {
        if (b[i + 6] != '-' || b[i + 16] != '_' || b[i + 19] != '-') continue;
        int ok = 1;
        for (int k = 0; k < 19 && ok; k++) {
            unsigned char c = (unsigned char)b[i + k];
            if (c < '!' || c > '~') ok = 0;
        }
        if (!ok) continue;
        snprintf(out, outsz, "%.9s", b + i + 7);
        break;
    }
    free(b);
}

/* Stop and unregister one task. Stop first: unregistering a task that is still transferring is
   how you get a half-written package left on the drive. Both codes are ignored on purpose - a
   task that is already stopped answers non-zero and that is a success for our purposes. */
/* Defined further down with the other title-id helpers; needed here so an install can release the
   tasks of the title it is about to replace. */
static void tid_from_cid(const char *cid, char *out, size_t outsz);

static void bgft_release(OrbisBgftTaskId task) {
    if (task == BGFT_INVALID_TASK_ID) return;
    if (bgft_stop_fn)  bgft_stop_fn(task);
    if (bgft_unreg_fn) bgft_unreg_fn(task);
}

/* Free stranded tasks of ours. `keep` is the live job's task, or BGFT_INVALID_TASK_ID.
   Returns how many were released.
 *
 * A FINISHED TASK IS THE TITLE'S LAUNCH TICKET. THIS IS THE WHOLE REASON THE ICON NEVER OPENED.
 *
 * Measured on 13.52. Pressing an installed title made ShellCore call sceBgftNotifyGameWillStart,
 * BGFT answered `[BGFT] ERROR: [3568] task not found. (PKGM00001)`, that call returned 0x80990019,
 * and ShellCore unmounted the package it had ALREADY MOUNTED SUCCESSFULLY and refused to start it -
 * CE-32930-7 on screen. Leaving one task registered for the title and pressing again:
 *
 *     [BGFT] [606] GameWillStart(PKGM00001, 2) start
 *     [BGFT] [576] task(00000075) PKGM00001            <- found instead of "task not found"
 *     [Syscore App] createApp PKGM00001
 *     EXEC /app0/eboot.bin [user]
 *
 * It launched. So this sweep - written to stop the table filling up, which is a real measured
 * problem - was also quietly making every title it had installed unlaunchable, games included.
 *
 * The rule: a task whose title actually has an app.pkg on disk is KEPT - that is its launch ticket.
 * Only tasks whose title installed nothing (failed, abandoned, orphaned by a payload reload) are
 * released here, because those are pure waste.
 *
 * WHAT DOES NOT HAPPEN HERE IS RECLAIMING UNDER "TABLE PRESSURE", because there is no measured
 * table pressure. This project believed for a long time that `/user/bgft/task` had about twelve
 * slots and that 0x80990086 meant it was full. klog says otherwise, in words:
 *
 *     [BGFT] ERROR: [2283] SCE_BGFT_ERROR_CONTENT_ALREADY_DOWNLOADING
 *
 * printed for the very register that answered 0x80990086. The conflict is PER CONTENT ID, not a
 * global count - which is also why releasing tasks always appeared to "make room": the task being
 * released was the one holding that content id. Measured since: registering worked fine with
 * thirteen directories in the table. So nothing is given up to make room, and no title loses its
 * place on the home screen so that another can install. See bgft_release_title(). */
static int bgft_sweep_ours(OrbisBgftTaskId keep) {
    if (!bgft_unreg_fn) return 0;
    DIR *d = opendir(BGFT_TASK_ROOT);
    if (!d) return 0;

    int freed = 0, kept = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        /* the directory name is the task id in hex, which is how a task we no longer hold an id
           for is addressable at all */
        char *end = NULL;
        long id = strtol(e->d_name, &end, 16);
        if (!end || *end || id < 0 || id > 0x7fffffff) continue;
        if ((OrbisBgftTaskId)id == keep) continue;
        if (!bgft_task_is_ours(e->d_name)) continue;
        char tid[16];
        bgft_task_title(e->d_name, tid, sizeof(tid));
        if (tid[0] && installed_app_pkg(tid) > 0) { kept++; continue; }   /* its launch ticket */
        bgft_release((OrbisBgftTaskId)id);
        freed++;
    }
    closedir(d);
    if (freed)
        ilog("install: released %d task%s of ours that installed nothing%s",
             freed, freed == 1 ? "" : "s",
             kept ? " (kept the ones their titles need to open)" : "");
    return freed;
}

/* How many of our BGFT tasks name this title - i.e. does it still have a launch ticket?
 *
 * Needed because a title can be perfectly installed and still refuse to open. Measured after a
 * console restart: our app's app.pkg, app.pbm, app.json and app.xml were all present and byte-correct
 * and app.db listed it at 01.04, yet pressing the icon did nothing - because the task table had been
 * emptied of our tasks and ShellCore's sceBgftNotifyGameWillStart answers "task not found", which is
 * fatal (see bgft_sweep_ours). Bytes are not launchability. */
static int ticket_count_for(const char *tid) {
    if (!tid || !tid[0]) return 0;
    /* SAME REASON AS console_titles_cached: this reads every task's d0.pdb - a dozen small files -
       and /api/tile/status is polled. The count only changes when an install does something, and
       every one of those paths drops the cache. */
    static pthread_mutex_t lk = PTHREAD_MUTEX_INITIALIZER;
    static char  last_tid[16] = {0};
    static int   last_n = -1;
    static long long last_at = 0;
    pthread_mutex_lock(&lk);
    if (last_n >= 0 && !strcmp(last_tid, tid) && now_ms() - last_at < TITLES_TTL_MS) {
        int cached = last_n;
        pthread_mutex_unlock(&lk);
        return cached;
    }
    pthread_mutex_unlock(&lk);

    DIR *d = opendir(BGFT_TASK_ROOT);
    if (!d) return 0;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        char *end = NULL;
        long id = strtol(e->d_name, &end, 16);
        if (!end || *end || id < 0) continue;
        char t[16];
        bgft_task_title(e->d_name, t, sizeof(t));
        if (t[0] && !strcmp(t, tid)) n++;
    }
    closedir(d);
    pthread_mutex_lock(&lk);
    snprintf(last_tid, sizeof(last_tid), "%s", tid);
    last_n = n;
    last_at = now_ms();
    pthread_mutex_unlock(&lk);
    return n;
}

/* Release the tasks belonging to ONE title, which is what has to happen before that same title is
 * installed again.
 *
 * Keeping a finished task is what makes a title launchable, and it is also what makes BGFT refuse
 * to register the title a second time. Measured, updating our own app from 01.03 to 01.04 with its
 * ticket in place:
 *
 *     register failed rc=0x80990088
 *     [BGFT] ERROR: [2289] SCE_BGFT_ERROR_SAME_APPLICATION_ALREADY_INSTALLED
 *
 * Our register call carries a content id and a size, not a version, so BGFT cannot tell an update
 * from a pointless reinstall - it sees a task saying this application is already here and stops.
 * Every earlier update succeeded only because the old sweep had just deleted that task, which is
 * also what left the title unlaunchable afterwards.
 *
 * So the ticket for the title being installed goes, and nothing else does. It is about to be
 * replaced by the new install's own task, so nothing is lost even if the install then fails - and
 * no OTHER title pays for it. */
static int bgft_release_title(const char *tid) {
    if (!bgft_unreg_fn || !tid || !tid[0]) return 0;
    DIR *d = opendir(BGFT_TASK_ROOT);
    if (!d) return 0;
    int freed = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        char *end = NULL;
        long id = strtol(e->d_name, &end, 16);
        if (!end || *end || id < 0 || id > 0x7fffffff) continue;
        if (!bgft_task_is_ours(e->d_name)) continue;
        char t[16];
        bgft_task_title(e->d_name, t, sizeof(t));
        if (strcmp(t, tid)) continue;
        bgft_release((OrbisBgftTaskId)id);
        freed++;
    }
    closedir(d);
    if (freed) {
        titles_cache_drop();
        ilog("install: %s is being installed again - released its %d old task%s first",
             tid, freed, freed == 1 ? "" : "s");
    }
    return freed;
}

/* Register + start one download-install. Returns 0 on success.
 *
 * `cid` is the package's CONTENT id (EP0786-CUSA02365_00-RIPTIDEGP2PS4001), not its title id, and
 * `size` is the real file size. Both come from the companion, which has already parsed the PKG
 * header - the console cannot read a header it has not downloaded yet. Passing a title id here
 * (which is what a filename gives you) is refused by the firmware with 0x80990008, and a zero
 * size is not enough for the task to be accepted either. */
static int bgft_install_url(const char *uri, const char *label, const char *cid, long long size,
                            const char *ptype, char *err, size_t errsz, OrbisBgftTaskId *out_task) {
    if (!g_bgft_ready) {
        snprintf(err, errsz, "This console cannot start installs - the background transfer service did not start");
        return -1;
    }
    int user_id = 0;
    if (sceUserServiceGetForegroundUser(&user_id) != 0 || user_id == 0)
        sceUserServiceGetInitialUser(&user_id);

    char fallback[16] = {0};
    if (!cid || !*cid) pkg_content_id_from_url_name(label && *label ? label : uri, fallback, sizeof(fallback));

    /* Always before registering, never after a failure: a sweep is cheap and idempotent, so the
       shop cannot be stopped by its own history. Two different things happen here, and conflating
       them is what made this hard to see. The sweep drops tasks that installed nothing. The release
       drops the tasks of the ONE title about to be installed, because BGFT will not register a
       content id that already has a task - and nothing else is touched. */
    bgft_sweep_ours(BGFT_INVALID_TASK_ID);
    char want_tid[16] = {0};
    tid_from_cid(cid, want_tid, sizeof(want_tid));

    OrbisBgftDownloadParam p;
    memset(&p, 0, sizeof(p));
    p.userId = user_id;
    p.entitlementType = 5;
    p.id = (cid && *cid) ? cid : fallback;
    p.contentUrl = uri;
    p.contentName = (label && *label) ? label : "PKG MUTANT SHOP";
    p.iconPath = "";
    p.playgoScenarioId = "0";
    p.option = ORBIS_BGFT_TASK_OPT_DISABLE_CDN_QUERY_PARAM;
    p.packageType = (ptype && *ptype) ? ptype : "PS4GD";
    p.packageSubType = "";
    /* THIS FIELD IS 32 BITS IN THE FIRMWARE'S OWN STRUCT - see bgft.h, which is the canonical
       OpenOrbis layout - so a package over 4 GiB is necessarily registered modulo 2^32 and there
       is nothing to widen. It is not the corruption it looks like: BGFT sizes the transfer from
       the HTTP Content-Length, and an 11,409,948,672-byte game registered here as 2,820,014,080
       still installed byte-for-byte correctly (verified at eight offsets spanning the whole file
       with tools/verify_console_install.py). The value is advisory - free-space estimate and the
       progress UI - and the finished check uses g_job.expect, which is the untruncated size. */
    p.packageSize = (uint32_t)(size > 0 ? size : 0);

    OrbisBgftTaskId task = BGFT_INVALID_TASK_ID;
    int rc = bgft_register_fn(&p, &task);
    /* RELEASE THE TITLE'S OLD TASK ONLY WHEN THAT IS WHAT IS IN THE WAY.
       This used to release it BEFORE registering, and a register that then failed for some unrelated
       reason left the title with no task at all - which is to say unlaunchable, because the console
       will not start a title BGFT has no task for. Measured: with PlayStation Network unreachable,
       register answers 0x80991404 ("CDN Auth Expired" in klog) and our own app lost the ticket it
       already had and could not get it back. The conflict codes are the only ones a release can fix,
       so it is spent on those and nothing else. */
    if (((unsigned)rc == 0x80990086u || (unsigned)rc == 0x80990088u) && want_tid[0]) {
        if (bgft_release_title(want_tid) > 0) rc = bgft_register_fn(&p, &task);
    }
    /* TWO REFUSALS WORTH SAYING IN WORDS, because their codes have been misread here before.
       0x80990086 is CONTENT_ALREADY_DOWNLOADING and 0x80990088 is SAME_APPLICATION_ALREADY_INSTALLED
       - both mean "a task for this content id is in the way", and the release above should already
       have cleared ours. Reaching here means the task belongs to something else, most likely the
       console's own Store download for the same title, and that is not ours to remove. */
    if ((unsigned)rc == 0x80990086u || (unsigned)rc == 0x80990088u) {
        snprintf(err, errsz,
                 "The PS4 is already handling this title itself - check its Downloads, cancel what "
                 "is there, then try again");
        ilog("install: refused rc=0x%08X (a task for %s already exists and is not ours) id=%.48s",
             (unsigned)rc, want_tid[0] ? want_tid : "this title", cid ? cid : "");
        return -2;
    }
    if (rc != 0) {
        /* NO CODE IN THE SENTENCE. It is on screen, often on a television, and it means nothing
           to the person reading it; the install log below keeps every one of them. */
        snprintf(err, errsz, "The console refused this package. The install log on the PS4 says why");
        ilog("install: register failed rc=0x%08X id=%.48s type=%s size=%lld uri=%.140s",
             (unsigned)rc, p.id ? p.id : "", p.packageType, size, uri);
        return -2;
    }
    rc = bgft_start_fn(task);
    if (rc != 0) {
        snprintf(err, errsz, "The console accepted the package but would not start it. "
                             "The install log on the PS4 says why");
        ilog("install: start failed rc=0x%08X task=%d", (unsigned)rc, (int)task);
        if (bgft_unreg_fn) bgft_unreg_fn(task);
        return -3;
    }
    if (out_task) *out_task = task;
    ilog("install: started task=%d id=%.48s uri=%.160s", (int)task, p.id ? p.id : "?", uri);
    return 0;
}

/* Defined below, with the app.db reader it uses. Declared here because job_refresh needs it. */
static int console_lists_title(const char *tid);
/* Both defined with the dashboard-app code further down; job_refresh needs them so that a finished
   install of the icon records itself, whoever started it. Not in the LITE build: that payload is
   the one that travels INSIDE the package, so it carries no package to compare against and has no
   staleness to record. */
#ifndef PMS_LITE
static void tile_repair_mark(void);
static int  tile_repair_tried(void);
#endif

/* Refresh g_job from the console's own progress. Called by /api/engine/job. */
static void job_refresh(void) {
    pthread_mutex_lock(&g_job_lock);
    /* A FINISHED JOB IS FINISHED. Once we have handed the task back, BGFT no longer knows it and
       answers every progress call with an error - which read as "no error, no bytes yet" and
       rewrote a job that had already failed back to "downloading". The verdict is reached once. */
    if (!g_job.active || g_job.released) { pthread_mutex_unlock(&g_job_lock); return; }
    /* A CLAIMED SLOT HAS NOTHING TO ASK ABOUT YET. The registration is still in flight, so
       g_job.task is BGFT_INVALID_TASK_ID and handing that to the service is the one thing this
       design will not do - see the claim block beside g_job. The commit is milliseconds away.

       The deadline is insurance. A claim can only leak if a thread dies between claiming and
       committing, which would take this whole daemon with it - but a slot that could then never be
       freed except by reloading the payload is a lock-out, and this shop has shipped one of those
       before. Three minutes, then the row goes back to exactly what it was. */
    if (g_job_claim) {
        if (g_job_claim_ms && now_ms() - g_job_claim_ms > JOB_CLAIM_TIMEOUT_MS) {
            g_job = g_job_before;
            g_job_claim = 0;
            ilog("job: a claim was never committed - the slot has been put back");
        }
        pthread_mutex_unlock(&g_job_lock);
        return;
    }
    OrbisBgftTaskId task = g_job.task;
    char tid[16];
    snprintf(tid, sizeof(tid), "%s", g_job.tid);
    pthread_mutex_unlock(&g_job_lock);

    long long done = 0, total = 0;
    int have = 0;
    unsigned err = 0;
    /* A DIRECT INSTALL HAS NO BGFT TASK, so asking BGFT about it answers an error that would be
       read as a failure. Its progress comes from the console's own installer, and its VERDICT comes
       from app.pkg changing on disk - the same proof the BGFT lane uses and the only one that has
       ever been trustworthy here. */
    int direct = 0;
    pthread_mutex_lock(&g_job_lock);
    direct = g_job.direct;
    char cid_copy[64];
    snprintf(cid_copy, sizeof(cid_copy), "%s", g_job.cid);
    unsigned direct_rc = g_job.rc;
    pthread_mutex_unlock(&g_job_lock);
    if (direct) {
        if (ai_progress_info_fn && cid_copy[0]) {
            unsigned st8 = 0, pc = 0, dsz = 0, tsz = 0, rs = 0;
            if (ai_progress_info_fn(cid_copy, &st8, &pc, &dsz, &tsz, &rs) == 0 && tsz) {
                done = (long long)dsz;
                total = (long long)tsz;
                have = 1;
            }
        }
        /* ONLY AN SCE-SHAPED CODE IS A FAILURE. sceAppInstUtilAppInstallPkg does not answer 0 for
           success: it answered 0x00000064 - one hundred - for an install that demonstrably worked,
           putting all 737,869,824 bytes of the package at /user/app/CUSA58072/app.pkg and the
           game's artwork under /user/appmeta. Treating non-zero as failure reported a completed
           install as an error. Every real error this console has produced has the top bit set
           (0x80020012 when the installer could not see the file, 0x8099xxxx from BGFT), so that is
           the test - and the verdict still comes from app.pkg changing on disk, not from here. */
        err = (direct_rc & 0x80000000u) ? direct_rc : 0;
    } else if (bgft_progress_fn) {
        OrbisBgftTaskProgress pr;
        memset(&pr, 0, sizeof(pr));
        if (bgft_progress_fn(task, &pr) == 0) {
            done  = (long long)pr.transferredTotal;
            total = (long long)pr.lengthTotal;
            err   = (unsigned)pr.errorResult;
            have  = 1;
        }
    }
    char jcat[8];
    pthread_mutex_lock(&g_job_lock);
    snprintf(jcat, sizeof(jcat), "%s", g_job.cat);
    pthread_mutex_unlock(&g_job_lock);
    long long onDisk = 0, onDiskMtime = 0;
    if (tid[0]) title_proof_facts(tid, jcat, &onDisk, &onDiskMtime);

    pthread_mutex_lock(&g_job_lock);
    /* BEFORE the assignment below, because the movement test further down compares against it. It
       used to read g_job.done after this line had already set it to `done`, i.e. `done > done`. */
    long long prev_done = g_job.done;
    /* THE COUNT ONLY GOES FORWARD. The progress read happens outside this lock, so two polls can
       land out of order and store an older count over a newer one - and the owner watches the
       percentage go down. Keeping the larger is honest: BGFT's counter does not rewind. */
    if (have) {
        if (done > g_job.done) g_job.done = done;
        g_job.total = total;
    }
    if (total > g_job.seen_total) g_job.seen_total = total;
    /* WHAT COUNTS AS DONE. app.pkg on disk at the expected size is the honest proof, exactly as on
       the PS5 side - the console's own progress counter is useful for showing movement but has
       been seen sitting at 0 for a transfer that had already finished, so it is never the verdict
       on its own. Where we know the size we require it; where we do not, any bytes plus a task
       that BGFT no longer reports is a finished install. */
    /* A DIRECT INSTALL NEEDS A DEADLINE. It has no task for BGFT to fail, so if the console simply
       never writes app.pkg - the metadata-only outcome the PS5 side warns about - the job would sit
       at "installing" until the payload was reloaded, and nothing would ever say why. Twenty minutes
       is far longer than any local install measured here (the largest so far finished in seconds
       once the path was right) and short enough that a person is not left staring at it. */
    if (direct && !err && g_job.started_ms &&
        now_ms() - g_job.started_ms > 20LL * 60 * 1000) {
        snprintf(g_job.state, sizeof(g_job.state), "error");
        snprintf(g_job.msg, sizeof(g_job.msg),
                 "The console started installing this and never finished. Nothing was changed - "
                 "try it again, or install it from the PC");
        g_job.released = 1;
        pthread_mutex_unlock(&g_job_lock);
        return;
    }

    /* A JOB THAT STOPS MOVING HAS TO SAY SO. Measured: an update whose finished check looked in the
       wrong place sat at "installing" for ever, and the PC showed 99% with nothing behind it. Even
       with the check corrected, a console can accept a package and quietly do nothing, and silence
       is the one answer this shop must never give. Movement is more bytes transferred OR the title's
       own file changing; fifteen minutes without either, on a transfer the service is not reporting
       an error for, is a stall. Generous, because a large game on a slow link is slow, not stuck. */
    if (have && done > prev_done) g_job.last_move_ms = now_ms();
    /* AGAINST THE PREVIOUS POLL, not the start of the job. Against the baseline this fired on every
       poll once the file had changed once (so a dead transfer could never be detected) and never at
       all while BGFT was still staging into /user/bgft/task/<id>/ (so a healthy download was on a
       fifteen-minute wall clock from handoff). */
    if (!g_job.last_seen || onDisk != g_job.last_size || onDiskMtime != g_job.last_mtime) {
        g_job.last_move_ms = now_ms();
        g_job.last_size = onDisk;
        g_job.last_mtime = onDiskMtime;
        g_job.last_seen = 1;
    }
    /* A STALL IS "NOTHING IS HAPPENING", NOT "I CANNOT TELL" - and the first version of this guard
       asked the wrong question. It asked whether the console LISTS the title, which for an update, an
       add-on or a reinstall is true before the transfer even starts: the guard always held and a dead
       one could never be detected. It was written for the case where a large FIRST install finished
       and its app.pkg did not match the package size.
       So: a job whose package file has changed since it started is not stalled, whatever the size
       says - and the app.db fallback stays for a first install, which is the case it is for. */
    int file_moved = onDisk > 0 && (!g_job.base_had ||
                                    onDisk != g_job.base_size || onDiskMtime != g_job.base_mtime);
    int looks_alive = file_moved || (!g_job.base_had && g_job.tid[0] && console_lists_title(g_job.tid));
    if (!err && g_job.last_move_ms && now_ms() - g_job.last_move_ms > 15LL * 60 * 1000
        && !looks_alive) {
        snprintf(g_job.state, sizeof(g_job.state), "error");
        snprintf(g_job.msg, sizeof(g_job.msg),
                 "This install stopped making progress and the console has not said why. Nothing "
                 "was changed - try it again");
        g_job.released = 1;
        pthread_mutex_unlock(&g_job_lock);
        return;
    }
    long long want = g_job.expect > 0 ? g_job.expect : 0;
    /* THE FILE HAS TO HAVE CHANGED, not merely be present - see app_pkg_facts. With no file at the
       start (a first install) this is the same test as before: any file of the right size is the
       install. With one already there (an update) nothing counts until the console replaces it. */
    int replaced = onDisk > 0 && (!g_job.base_had ||
                                  onDisk != g_job.base_size || onDiskMtime != g_job.base_mtime);
    int big_enough = replaced && (want <= 0 || onDisk >= (want - want / 50));

    /* THE SIZE TEST IS NOT THE ONLY WAY AN INSTALL CAN BE FINISHED.
     *
     * Measured: BO3 downloaded, installed and ran, and this said it stopped at 99%. Its installed
     * app.pkg is 43.6 GB and need not land within 2% of the package we handed over - a console
     * writing a large base game does not owe us a byte-identical file. When the size test misses, the
     * job never leaves "transferring", so it shows BGFT's last byte count for fifteen minutes and is
     * then failed by the stall detector, having actually succeeded.
     *
     * So there is a second route, and it is three facts together because no ONE of them is safe:
     *   done >= total       the console says it moved every byte it asked for - but a counter can
     *                       reach its total before the install step has run;
     *   replaced            this package file really changed - but so does a download in progress;
     *   console_lists_title the console's own database has the title - but a registered title can be
     *                       a phantom with no bytes behind it, which this project has already shipped
     *                       once and written a memory about.
     * Each alone is a false positive. All three at once is what "installed" means.
     *
     * The database read is why this is guarded: it happens only when the cheap size test has already
     * failed AND the transfer is otherwise complete, so the ordinary polling path is untouched. */
    if (!big_enough && replaced && !err && g_job.total > 0 && g_job.done >= g_job.total
        && tid[0] && console_lists_title(tid)) {
        big_enough = 1;
        ilog("install: %s - the size test missed (%lld on disk vs %lld expected) but the console "
             "transferred every byte and lists the title, so it is installed",
             tid, (long long)onDisk, (long long)want);
    }

    if (err) {
        g_job.rc = err;
        snprintf(g_job.state, sizeof(g_job.state), "error");
        /* WHY IT STOPPED, WHEN WE CAN ACTUALLY TELL.
           A console that can reach PlayStation Network checks every title for an update before it
           downloads anything. When it finds one it tries to MERGE that update into the install, and
           it cannot fetch it - the package came from here, not from the store - so it abandons the
           whole thing with nothing written.

           0x80990004 IS THAT CASE, and it is measured twice on this console rather than guessed.
           The second time the console spelled the sequence out in klog:

               [ScePatchChecker] check (title_id='CUSA14409', app_version='01.00'): status=1
               [BGFT] Patch Check [UP2015-CUSA14409_00-CASTLECRASHERSNA]
               [PATCH MERGE] : CheckDeltaPatchInfo error. [0x80f00640]
               Task 0000007c ... ended (state=0,runstate=2,error=0x80990004)

           status=1 is PlayStation Network saying a newer version exists. The first time, the only
           fingerprint we had was arithmetic - BGFT asked for 238,419,968 bytes against a
           227,540,992-byte package, the difference being the update - and that test is kept because
           it catches the case where the code differs.

           It still has to be paired with "nothing landed on disk": a code on its own is not a
           diagnosis, and this project has twice shipped a confident wrong one. Anything else gets
           the plain sentence. */
        int psn_patch_check = onDisk <= 0 &&
                              ((unsigned)err == 0x80990004u ||
                               (want > 0 && g_job.seen_total > want + (1LL << 20)));
        if (psn_patch_check)
            snprintf(g_job.msg, sizeof(g_job.msg),
                     "The PS4 found a newer version of this game on PlayStation Network and tried to "
                     "merge it instead of installing this package. Add the update package to your "
                     "library and install it too, or stop the console reaching PlayStation Network, "
                     "then try again");
        else
            snprintf(g_job.msg, sizeof(g_job.msg),
                     "The console stopped this install. The install log on the PS4 says why");
    } else if (big_enough) {
        /* Something just appeared on this console, so a remembered list of what it has is wrong. */
        titles_cache_drop();
        /* ...and so is the decision about which titles carry the in-game helper. The owner installed
           a game the library has cheats for and had to arm it by hand, because that decision was only
           ever made at boot. Deferred to the watcher: this runs under g_job_lock. */
        g_autoarm_dirty = 1;
        snprintf(g_job.state, sizeof(g_job.state), "installed");
        snprintf(g_job.msg, sizeof(g_job.msg), "Installed on this PS4");
        /* A FINISHED REPAIR OF THE ICON RECORDS ITSELF, whoever started it. The PC's lane is the
           one that works here - the console's own is refused with 0x80991404 - and nothing was
           marking it, so /api/tile/status went on reporting the icon stale after every successful
           repair and the PC reinstalled it every ten minutes for ever. */
#ifndef PMS_LITE
        if (!strcmp(g_job.tid, PS4_TILE_TID)) tile_repair_mark();
#endif
        /* AND THE CONSOLE HAS TO AGREE THAT IT HAS IT. app.pkg appearing proves the files arrived;
           it does not prove the console registered the title, and the two really can disagree - a
           package written to /user/app with no row in app.db is a title that exists on disk and
           nowhere else, which is the "phantom install" this project has been caught by before. The
           state stays "installed" because the install genuinely happened; the sentence stops
           short of promising it is playable. */
        if (direct && tid[0] && !console_lists_title(tid))
            snprintf(g_job.msg, sizeof(g_job.msg),
                     "The files are on this PS4 but the console has not listed it yet - "
                     "restart the console, and if it is still missing install it again");
    } else {
        snprintf(g_job.state, sizeof(g_job.state), "downloading");
        snprintf(g_job.msg, sizeof(g_job.msg), "The PS4 is downloading and installing it");
    }
    /* THE VERDICT IS REACHED ONCE - but only a FAILED job gives its task back.
       A task that installed something is what lets that title open from the home screen at all
       (see bgft_sweep_ours), so handing it back here is how this shop used to install a game and
       silently make it unlaunchable. A failed job installed nothing, so its task is pure waste and
       goes immediately. `released` now means "the verdict is in, stop re-reading it". */
    int over   = strcmp(g_job.state, "downloading") != 0;
    int failed = !strcmp(g_job.state, "error");
    OrbisBgftTaskId spent = (over && failed && !g_job.released) ? g_job.task : BGFT_INVALID_TASK_ID;
    if (over) g_job.released = 1;
    pthread_mutex_unlock(&g_job_lock);
    if (spent != BGFT_INVALID_TASK_ID) bgft_release(spent);
}

/* THE CATEGORY AND THE EXPECTED SIZE the finished check needs, from the package type the companion
   named. Call with g_job_lock HELD, before job_baseline_locked().

   `cat` is WHERE title_proof_facts looks: "gp" is /user/patch/<TID>/patch.pkg, "ac" is an add-on's
   ac.pkg, "gd" is the game's own app.pkg. `expect` is what the size test compares against. Four start
   paths set both; the queue release and the retry set neither, so a queued or retried UPDATE looked
   for a game's app.pkg - which an update never writes - and ran to completion with the row still
   saying "downloading". One function now, so a fifth path cannot forget. */
static void job_set_kind_locked(const char *ptype, long long psize, const char *cid) {
    g_job.expect = psize;
    if (ptype) {
        if (!strcmp(ptype, "PS4GP"))      snprintf(g_job.cat, sizeof(g_job.cat), "gp");
        else if (!strcmp(ptype, "PS4AC")) snprintf(g_job.cat, sizeof(g_job.cat), "ac");
        else if (!strcmp(ptype, "PS4GD")) snprintf(g_job.cat, sizeof(g_job.cat), "gd");
    }
    if (!g_job.tid[0] && cid) tid_from_cid(cid, g_job.tid, sizeof(g_job.tid));
}

/* Remember what app.pkg looked like before an install starts. Call with g_job_lock HELD, straight
   after g_job.tid is final: the finished check is only as honest as this snapshot. */
static void job_baseline_locked(void) {
    g_job.base_had = g_job.tid[0]
                     ? title_proof_facts(g_job.tid, g_job.cat, &g_job.base_size, &g_job.base_mtime)
                     : 0;
    g_job.last_size = g_job.base_size;
    g_job.last_mtime = g_job.base_mtime;
    g_job.last_seen = 1;
    g_job.last_move_ms = now_ms();
}

/* ------------------------------------------- packages the console already has
 *
 * A PS4 with a stick full of PKGs has to be installable with this PC switched off - that is what
 * the PS5 build's /api/install does, and PS4 owners keep packages on USB far more often than PS5
 * owners do. THE PS5'S SHAPE IS REUSED DELIBERATELY: the package is served over HTTP and the
 * console's own installer fetches it, because that is the pipeline that produces a playable game.
 * Registering a local path with an install call instead is what left a tile that crashes on launch
 * (see the note above /api/install in ps5-app/onconsole/server.c - it cost a console).
 *
 * The PS5 needed a whole second server for this, since its shop answers on one accept loop and a
 * multi-gigabyte transfer through it would freeze the UI. Ours gives every connection its own
 * thread, so the stream rides the normal port: /pkgfile/<token>, registered below.
 */
#define LOCALPKG_MAX 16
static pthread_mutex_t g_lp_lock = PTHREAD_MUTEX_INITIALIZER;
static struct { char path[1024]; } g_lp[LOCALPKG_MAX];
static int g_lp_n;

/* Tokens rather than the path in the URL. BGFT re-requests the URL on its own schedule, so a path
   with spaces or brackets in it would have to survive re-encoding every time; and a path in a URL
   is an invitation to ask this server to open any file on the console. Returns the token, or -1. */
static int localpkg_register(const char *path) {
    pthread_mutex_lock(&g_lp_lock);
    for (int i = 0; i < g_lp_n; i++)
        if (!strcmp(g_lp[i].path, path)) { pthread_mutex_unlock(&g_lp_lock); return i; }
    int t = -1;
    if (g_lp_n < LOCALPKG_MAX) {
        t = g_lp_n++;
        snprintf(g_lp[t].path, sizeof(g_lp[t].path), "%s", path);
    }
    pthread_mutex_unlock(&g_lp_lock);
    return t;
}

static int localpkg_path(int token, char *out, size_t outsz) {
    int ok = 0;
    pthread_mutex_lock(&g_lp_lock);
    if (token >= 0 && token < g_lp_n) { snprintf(out, outsz, "%s", g_lp[token].path); ok = 1; }
    pthread_mutex_unlock(&g_lp_lock);
    return ok;
}

/* ---------------------------------------------------- reading a PKG's own header
 *
 * The console will not accept an install task without the content id, the real size and the right
 * package type, and for a package that is already on this console there is no companion to supply
 * them - so they are read out of the file, the same way companion/pkg_meta.py reads them. The PKG
 * header is BIG-endian, the param.sfo inside it is LITTLE-endian, and in an fpkg that param.sfo
 * (entry id 0x1000) is stored in the clear.
 *
 * CATEGORY is what decides the package type, and it is the file describing itself rather than a
 * guess of ours: gd = game, gp = game patch, ac = additional content. Measured across this library,
 * a base game and its update are BOTH content_type 0x1A and differ only in flag bits, so reading
 * the type out of the header alone would have called every update a base game.
 */
#define PKG_MAGIC_PS4 0x7F434E54u
#define PKG_ENTRY_PARAM_SFO 0x1000u

static unsigned be32(const unsigned char *p) {
    return ((unsigned)p[0] << 24) | ((unsigned)p[1] << 16) | ((unsigned)p[2] << 8) | (unsigned)p[3];
}
static unsigned le32(const unsigned char *p) {
    return ((unsigned)p[3] << 24) | ((unsigned)p[2] << 16) | ((unsigned)p[1] << 8) | (unsigned)p[0];
}
static unsigned le16(const unsigned char *p) {
    return ((unsigned)p[1] << 8) | (unsigned)p[0];
}

/* One key out of a param.sfo. Strings only - every field we want is one. */
static void sfo_find(const unsigned char *sfo, long n, const char *want, char *out, size_t outsz) {
    out[0] = 0;
    if (n < 0x14 || memcmp(sfo, "\0PSF", 4)) return;
    unsigned key_tbl = le32(sfo + 8), data_tbl = le32(sfo + 12), cnt = le32(sfo + 16);
    size_t wl = strlen(want);
    if (cnt > 1024) return;
    for (unsigned i = 0; i < cnt; i++) {
        long base = 0x14 + (long)i * 16;
        if (base + 16 > n) return;
        unsigned ko = le16(sfo + base), ln = le32(sfo + base + 4), doff = le32(sfo + base + 12);
        long kp = (long)key_tbl + (long)ko;
        if (kp < 0 || kp + (long)wl + 1 > n) continue;
        if (memcmp(sfo + kp, want, wl) || sfo[kp + (long)wl] != 0) continue;
        long dp = (long)data_tbl + (long)doff;
        if (dp < 0 || dp >= n) return;
        long avail = n - dp;
        long take = (long)ln < avail ? (long)ln : avail;
        if (take >= (long)outsz) take = (long)outsz - 1;
        if (take < 0) take = 0;
        memcpy(out, sfo + dp, (size_t)take);
        out[take] = 0;
        return;
    }
}

/* Fills whatever it can and returns 1 when the file really is a PS4 package. */
static int pkg_file_facts(const char *path, char *cid, size_t cidsz, char *cat, size_t catsz,
                          char *title, size_t titlesz, long long *out_size) {
    if (cid && cidsz) cid[0] = 0;
    if (cat && catsz) cat[0] = 0;
    if (title && titlesz) title[0] = 0;
    if (out_size) *out_size = 0;
    int f = open(path, O_RDONLY);
    if (f < 0) return 0;
    struct stat st;
    if (fstat(f, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size < 0x100) { close(f); return 0; }
    unsigned char head[0x80];
    if (read(f, head, sizeof(head)) != (ssize_t)sizeof(head)) { close(f); return 0; }
    if (be32(head) != PKG_MAGIC_PS4) { close(f); return 0; }
    if (out_size) *out_size = (long long)st.st_size;
    /* The content id is at 0x40 in the header, so even a package whose param.sfo we cannot reach
       still yields the one field an install task cannot do without. */
    if (cid && cidsz) {
        size_t k = 0;
        for (; k < 36 && k + 1 < cidsz && head[0x40 + k]; k++) cid[k] = (char)head[0x40 + k];
        cid[k] = 0;
    }
    unsigned ecount = be32(head + 0x10), toff = be32(head + 0x18);
    if (ecount && ecount <= 100000 &&
        (long long)toff + (long long)ecount * 32 <= (long long)st.st_size) {
        unsigned char *tbl = (unsigned char *)malloc((size_t)ecount * 32);
        if (tbl) {
            if (lseek(f, (off_t)toff, SEEK_SET) == (off_t)toff &&
                read(f, tbl, (size_t)ecount * 32) == (ssize_t)((size_t)ecount * 32)) {
                for (unsigned i = 0; i < ecount; i++) {
                    const unsigned char *e = tbl + (size_t)i * 32;
                    if (be32(e) != PKG_ENTRY_PARAM_SFO) continue;
                    unsigned off = be32(e + 16), sz = be32(e + 20);
                    if (!sz || sz > (1u << 20) || (long long)off + sz > (long long)st.st_size) break;
                    unsigned char *sfo = (unsigned char *)malloc(sz);
                    if (!sfo) break;
                    if (lseek(f, (off_t)off, SEEK_SET) == (off_t)off &&
                        read(f, sfo, sz) == (ssize_t)sz) {
                        char buf[160];
                        if (cat && catsz) {
                            sfo_find(sfo, (long)sz, "CATEGORY", buf, sizeof(buf));
                            snprintf(cat, catsz, "%s", buf);
                        }
                        if (title && titlesz) {
                            sfo_find(sfo, (long)sz, "TITLE", buf, sizeof(buf));
                            if (buf[0]) snprintf(title, titlesz, "%s", buf);
                        }
                        sfo_find(sfo, (long)sz, "CONTENT_ID", buf, sizeof(buf));
                        if (buf[0] && cid && cidsz) snprintf(cid, cidsz, "%s", buf);
                    }
                    free(sfo);
                    break;
                }
            }
            free(tbl);
        }
    }
    close(f);
    return 1;
}

/* CATEGORY -> what BGFT calls this kind of package. The same two letters, and the same mapping the
   companion uses in PS4_PACKAGE_TYPE, so a package installs identically from either side. */
static const char *pkg_type_for_category(const char *cat) {
    if (cat && !strncmp(cat, "gp", 2)) return "PS4GP";
    if (cat && !strncmp(cat, "ac", 2)) return "PS4AC";
    if (cat && !strncmp(cat, "al", 2)) return "PS4AL";
    if (cat && !strncmp(cat, "dp", 2)) return "PS4DP";
    return "PS4GD";                       /* gd, and anything we could not read */
}

static const char *kind_for_category(const char *cat) {
    if (cat && !strncmp(cat, "gp", 2)) return "update";
    if (cat && !strncmp(cat, "ac", 2)) return "dlc";
    return "base";
}

/* --------------------------------------------- packages on removable media
   /mnt/usb0..7, three levels deep. The companion asks for these through /api/library and lists
   them beside its own, so a stick is installable from any device with the PC switched off. Same
   contract as the PS5 build, whose console_usb_packages() reads exactly this. */
#define USBPKG_MAX 200

typedef struct {
    char path[1024];
    char file[256];
    char drive[12];
    char cid[64];
    char cat[16];
    char title[160];
    long long size;
} usbpkg_t;

static int usb_scan_dir(const char *dir, const char *drive, usbpkg_t *out, int max, int n, int depth) {
    if (n >= max || depth > 3) return n;
    DIR *d = opendir(dir);
    if (!d) return n;
    struct dirent *e;
    while ((e = readdir(d)) && n < max) {
        if (e->d_name[0] == '.') continue;
        char full[1024];
        snprintf(full, sizeof(full), "%s/%s", dir, e->d_name);
        struct stat st;
        if (stat(full, &st) != 0) continue;
        if (S_ISDIR(st.st_mode)) { n = usb_scan_dir(full, drive, out, max, n, depth + 1); continue; }
        size_t l = strlen(e->d_name);
        if (l < 5 || strcasecmp(e->d_name + l - 4, ".pkg")) continue;
        usbpkg_t *u = &out[n];
        memset(u, 0, sizeof(*u));
        snprintf(u->path, sizeof(u->path), "%s", full);
        snprintf(u->file, sizeof(u->file), "%.255s", e->d_name);
        snprintf(u->drive, sizeof(u->drive), "%.11s", drive);
        /* A file that is not really a PS4 package is not offered at all - better than a card that
           cannot install. This reads two small pieces of the file, not the whole thing. */
        if (!pkg_file_facts(full, u->cid, sizeof(u->cid), u->cat, sizeof(u->cat),
                            u->title, sizeof(u->title), &u->size))
            continue;
        n++;
    }
    closedir(d);
    return n;
}

static int usb_scan(usbpkg_t *out, int max) {
    int n = 0;
    for (int i = 0; i < 8 && n < max; i++) {
        char root[32], drv[12];
        snprintf(root, sizeof(root), "/mnt/usb%d", i);
        snprintf(drv, sizeof(drv), "usb%d", i);
        struct stat st;
        if (stat(root, &st) != 0 || !S_ISDIR(st.st_mode)) continue;
        n = usb_scan_dir(root, drv, out, max, n, 0);
    }
    return n;
}

/* Title id out of a content id (EP0786-CUSA02365_00-... -> CUSA02365), empty if there is none. */
static void tid_from_cid(const char *cid, char *out, size_t outsz) {
    out[0] = 0;
    const char *d = cid ? strchr(cid, '-') : NULL;
    if (!d) return;
    d++;
    size_t k = 0;
    while (d[k] && d[k] != '_' && k + 1 < outsz) { out[k] = d[k]; k++; }
    out[k] = 0;
}

/* --------------------------------------------------------------- the file API
   Same shapes as the PS5 build so the companion's fs_read/fs_list/fs_write work unchanged. */

static int fs_param_path(const char *rawpath, char *out, size_t outsz) {
    char enc[1024] = {0};
    if (!qparam(rawpath, "path", enc, sizeof(enc))) return 0;
    snprintf(out, outsz, "%s", enc);
    return out[0] == '/';
}

/* WHERE DELETE MAY ACT, AND NOWHERE ELSE.
 *
 * The PS5 build keeps a boundary of the same shape and lets it cover the drives, because on that
 * console a drive holds folders the app itself fills: the backup watch folders and the cheat
 * library. A PS4 is the other way round. Here the drives hold THE OWNER'S PACKAGES - usb_scan()
 * walks /mnt/usb0..7 three levels deep and every package it finds is a row in /api/library - so a
 * delete that reached them would be deleting the library this shop exists to serve. The PS5's
 * delete lane learned the same lesson from the other end: a drive root is not a folder that exists
 * to hold our things, and treating it as one hands the owner's own files the rules that were
 * written for a scan folder.
 *
 * Nothing on this console needs more than this. The only caller that has ever had to remove a file
 * here is the deploy and cleanup probe the tools write into the shop's own folder and then ask us
 * to take away again.
 *
 * The folder itself is not deletable either way it is spelt: the root is compared WITH its
 * trailing slash and something has to follow that slash, so neither /data/pkg-mutant-shop nor
 * /data/pkg-mutant-shop/ passes and only a path INSIDE it does. Reads and listings stay unbounded,
 * exactly as on the PS5 - the whole point of them is the console's own databases - but an
 * unauthenticated GET that could unlink any path on a root process is an exposure with no user. */
static int fs_deletable_path(const char *p) {
    if (!p || p[0] != '/' || strstr(p, "..")) return 0;
    size_t n = strlen(SHOP_DATA_DIR "/");
    return !strncmp(p, SHOP_DATA_DIR "/", n) && p[n] != 0;
}

static void fs_send_stat(int fd, const char *path) {
    struct stat st;
    if (stat(path, &st) != 0) {
        send_status(fd, "404 Not Found", "application/json", "{\"ok\":false,\"error\":\"no such path\"}");
        return;
    }
    char esc[1100], o[1400];
    json_escape(path, esc, sizeof(esc));
    snprintf(o, sizeof(o), "{\"ok\":true,\"path\":\"%s\",\"dir\":%s,\"size\":%lld,\"mtime\":%lld}",
             esc, S_ISDIR(st.st_mode) ? "true" : "false",
             (long long)st.st_size, (long long)st.st_mtime);
    send_json(fd, o);
}

#define FS_LIST_START (1 * 1024 * 1024)
#define FS_LIST_MAX   (8 * 1024 * 1024)

static void fs_send_list(int fd, const char *path) {
    DIR *d = opendir(path);
    if (!d) {
        send_status(fd, "404 Not Found", "application/json",
                    "{\"ok\":false,\"error\":\"cannot open directory\"}");
        return;
    }
    size_t cap = FS_LIST_START, len = 0;
    char *out = (char *)malloc(cap);
    if (!out) { closedir(d); send_json(fd, "{\"ok\":false,\"error\":\"oom\"}"); return; }
    len += (size_t)snprintf(out + len, cap - len, "{\"ok\":true,\"entries\":[");
    int wrote = 0, truncated = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (!strcmp(e->d_name, ".") || !strcmp(e->d_name, "..")) continue;
        char full[1200];
        snprintf(full, sizeof(full), "%s/%s", path, e->d_name);
        struct stat st;
        if (stat(full, &st) != 0) continue;
        char esc[600];
        json_escape(e->d_name, esc, sizeof(esc));
        if (len + strlen(esc) + 160 >= cap) {
            if (cap >= FS_LIST_MAX) { truncated = 1; break; }
            size_t ncap = cap * 2;
            if (ncap > FS_LIST_MAX) ncap = FS_LIST_MAX;
            char *n = (char *)realloc(out, ncap);
            if (!n) { truncated = 1; break; }
            out = n; cap = ncap;
        }
        len += (size_t)snprintf(out + len, cap - len,
                                "%s{\"name\":\"%s\",\"dir\":%s,\"size\":%lld,\"mtime\":%lld}",
                                wrote ? "," : "", esc, S_ISDIR(st.st_mode) ? "true" : "false",
                                (long long)st.st_size, (long long)st.st_mtime);
        wrote++;
    }
    closedir(d);
    len += (size_t)snprintf(out + len, cap - len, "],\"count\":%d,\"truncated\":%s}",
                            wrote, truncated ? "true" : "false");
    send_json(fd, out);
    free(out);
}

/* `req` is the whole request, so this can read a Range header; NULL means "no range", which is
   what every caller that does not have the request text passes. */
static void fs_send_read_ranged(int fd, const char *path, const char *req) {
    int f = open(path, O_RDONLY);
    if (f < 0) {
        send_status(fd, "404 Not Found", "application/json", "{\"ok\":false,\"error\":\"cannot open\"}");
        return;
    }
    struct stat st;
    if (fstat(f, &st) != 0 || !S_ISREG(st.st_mode)) {
        close(f);
        send_status(fd, "400 Bad Request", "application/json",
                    "{\"ok\":false,\"error\":\"not a regular file\"}");
        return;
    }

    /* EVERYTHING HERE IS 64-BIT ON PURPOSE. A PS4 game package is routinely over 4 GB - the one
       that prompted this is 11,409,948,672 bytes - and the reason this route exists at all is
       that GoldHEN's FTP reports such a file's size modulo 2^32 and refuses to seek past 2 GB.
       Repeating either mistake here would make this no better than the thing it replaces. */
    long long size = (long long)st.st_size;
    long long start = 0, end = size - 1;
    int partial = 0;

    const char *h = req ? strcasestr_local(req, "\nRange:") : NULL;
    if (h) {
        const char *p = strchr(h, ':');
        if (p) {
            p++;
            while (*p == ' ' || *p == '\t') p++;
            if (!strncasecmp(p, "bytes=", 6)) {
                p += 6;
                const char *dash = strchr(p, '-');
                if (dash) {
                    char a[32] = {0}, b[32] = {0};
                    size_t an = (size_t)(dash - p);
                    if (an < sizeof(a)) memcpy(a, p, an);
                    const char *q = dash + 1;
                    size_t bn = 0;
                    while (q[bn] && q[bn] != '\r' && q[bn] != '\n' && q[bn] != ',' && bn < sizeof(b) - 1) bn++;
                    memcpy(b, dash + 1, bn);
                    if (!a[0] && b[0]) {                 /* bytes=-N : the LAST N bytes */
                        long long n = strtoll(b, NULL, 10);
                        if (n > 0) { start = size > n ? size - n : 0; end = size - 1; partial = 1; }
                    } else if (a[0]) {
                        start = strtoll(a, NULL, 10);
                        if (b[0]) {
                            long long e = strtoll(b, NULL, 10);
                            if (e < end) end = e;
                        }
                        partial = 1;
                    }
                }
            }
        }
    }

    if (partial && (start < 0 || start >= size || start > end)) {
        char h416[200];
        int n416 = snprintf(h416, sizeof(h416),
                            "HTTP/1.1 416 Range Not Satisfiable\r\nContent-Range: bytes */%lld\r\n"
                            "Content-Length: 0\r\nAccess-Control-Allow-Origin: *\r\n"
                            "Connection: close\r\n\r\n", size);
        write_all(fd, h416, (size_t)n416);
        close(f);
        return;
    }

    long long length = end - start + 1;
    char hdr[400];
    int hn;
    if (partial) {
        hn = snprintf(hdr, sizeof(hdr),
                      "HTTP/1.1 206 Partial Content\r\nContent-Type: application/octet-stream\r\n"
                      "Content-Length: %lld\r\nContent-Range: bytes %lld-%lld/%lld\r\n"
                      "Accept-Ranges: bytes\r\nCache-Control: no-store\r\n"
                      "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n",
                      length, start, end, size);
    } else {
        hn = snprintf(hdr, sizeof(hdr),
                      "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                      "Content-Length: %lld\r\nAccept-Ranges: bytes\r\n"
                      "Cache-Control: no-store\r\n"
                      "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n",
                      size);
    }
    write_all(fd, hdr, (size_t)hn);

    if (start && lseek(f, (off_t)start, SEEK_SET) < 0) { close(f); return; }
    char small[BULK_BUF_SMALL];
    size_t bufsz = 0;
    char *buf = bulk_buf_get(small, &bufsz);
    long long left = length;
    while (left > 0) {
        size_t want = left > (long long)bufsz ? bufsz : (size_t)left;
        ssize_t r = read(f, buf, want);
        if (r <= 0) break;
        if (write_all(fd, buf, (size_t)r) != 0) break;
        left -= r;
    }
    bulk_buf_free(buf, small);
    close(f);
}

static void fs_send_read(int fd, const char *path) {
    fs_send_read_ranged(fd, path, NULL);
}

/* Streamed upload, .part then rename - a half-written file must never look finished. */
static int fs_recv_write(int cl, const char *rawpath, const char *req, int header_len, int have) {
    char path[1024] = {0};
    if (!fs_param_path(rawpath, path, sizeof(path))) {
        send_status(cl, "400 Bad Request", "application/json",
                    "{\"ok\":false,\"error\":\"absolute ?path= required\"}");
        return 0;
    }
    const char *cl_h = strcasestr_local(req, "content-length:");
    if (!cl_h) {
        send_status(cl, "411 Length Required", "application/json",
                    "{\"ok\":false,\"error\":\"content-length required\"}");
        return 0;
    }
    long long want = atoll(cl_h + 15);
    if (want < 0) want = 0;

    char part[1100];
    snprintf(part, sizeof(part), "%s.part", path);
    mkparents(part);
    int f = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (f < 0) {
        send_status(cl, "500 Internal Server Error", "application/json",
                    "{\"ok\":false,\"error\":\"cannot create file\"}");
        return 0;
    }
    long long got = 0;
    int failed = 0;
    if (have > 0) {
        write_all(f, req + header_len, (size_t)have);
        got = have;
    }
    char small[BULK_BUF_SMALL];
    size_t bufsz = 0;
    char *buf = bulk_buf_get(small, &bufsz);
    while (!failed && got < want) {
        size_t chunk = bufsz;
        if ((long long)chunk > want - got) chunk = (size_t)(want - got);
        ssize_t r = read(cl, buf, chunk);
        if (r <= 0) { failed = 1; break; }
        /* write_all, NOT write. A short write is legal and this treated it as fatal, discarding a
           transfer of tens of GB that had nothing wrong with it. Moving to a 1 MB buffer makes a
           partial write more likely, not less, so the two belong in the same change. */
        if (write_all(f, buf, (size_t)r) != 0) { failed = 1; break; }
        got += r;
    }
    bulk_buf_free(buf, small);
    close(f);
    if (failed || got != want) {
        unlink(part);
        char o[220];
        snprintf(o, sizeof(o), "{\"ok\":false,\"error\":\"short write\",\"written\":%lld,\"expected\":%lld}",
                 got, want);
        send_status(cl, "500 Internal Server Error", "application/json", o);
        return 0;
    }
    unlink(path);
    if (rename(part, path) != 0) {
        unlink(part);
        send_status(cl, "500 Internal Server Error", "application/json",
                    "{\"ok\":false,\"error\":\"rename failed\"}");
        return 0;
    }
    char esc[1100], o[1400];
    json_escape(path, esc, sizeof(esc));
    snprintf(o, sizeof(o), "{\"ok\":true,\"path\":\"%s\",\"size\":%lld}", esc, got);
    send_json(cl, o);
    return 0;
}

/* --------------------------------------------------------------- the library */

/* THESE THREE SCANS SHARE ONE BUFFER AND THIS SERVER IS THREADED.
 *
 * The PS5 payload keeps its scan buffer in a static safely because it answers on a single accept
 * loop. This one gives every connection its own thread, and a 512-entry title array is far too
 * large to put on a thread stack - so the buffers stay static and one lock covers every reader of
 * them. It is needed: the companion, the page on the television and a phone all poll /api/library,
 * and two of them landing together would have had one thread filling the array while another read
 * it. Not a hot path - the companion caches the answer for thirty seconds.
 */
static pthread_mutex_t g_scan_lock = PTHREAD_MUTEX_INITIALIZER;

/* Does the console's own database list this title? app.db is the console's answer to "what do I
   have", and it is a different question from "are there files on disk" - see job_refresh. */
/* A SHORT-LIVED ANSWER, BECAUSE THE QUESTION IS ASKED CONSTANTLY.
 *
 * read_console_titles() slurps the WHOLE of app.db into memory on every call - the file is megabytes
 * - and the companion polls /api/tile/status and the install job every few seconds. That is a
 * multi-megabyte malloc and free several times a minute, for ever, in a system daemon whose heap this
 * payload does not own and cannot compact.
 *
 * What that costs was measured today, and it is not theoretical: after about half an hour of polling,
 * a 256 KB allocation for the library JSON failed outright, and then accept() began failing on the
 * listening socket - 47,000 consecutive failures, the port still open, every request answered by a
 * reset. A kernel that cannot allocate a socket cannot accept one.
 *
 * Five seconds of staleness is invisible to a person watching an install and removes almost all of
 * that traffic. Anything that changes what is installed calls titles_cache_drop() so the next
 * question is answered from the console rather than from a memory of it. */
static pthread_mutex_t g_titles_lock = PTHREAD_MUTEX_INITIALIZER;
static ps4_title_t     g_titles[MAX_TITLES];
static int             g_titles_n = -1;
static long long       g_titles_at = 0;

/* BUMPED BY EVERY DROP, so a read that was already in flight cannot put its older answer back.
   console_titles_cached releases the lock while it reads app.db - it must, the read is slow and
   megabytes - and it used to store the result unconditionally on the way out. A drop landing in
   that window was silently undone, which for the autoarm decision means deciding from a snapshot
   taken before the game existed. */
static unsigned g_titles_gen = 0;

static void titles_cache_drop(void) {
    pthread_mutex_lock(&g_titles_lock);
    g_titles_n = -1;
    g_titles_gen++;
    pthread_mutex_unlock(&g_titles_lock);
}

/* Copies into the caller's array so nothing holds g_titles_lock while it works. */
/* NO g_scan_lock IN HERE, AND NOTHING SHARED TO PROTECT.
 *
 * This used to take g_scan_lock around its app.db read - and all three library builders call it from
 * inside a wrapper that is already holding that lock. A statically initialised mutex on this platform
 * does not block when the same thread locks it twice, so the INNER UNLOCK handed the caller's critical
 * section away, mid-scan, over the shared buffers the note beside g_scan_lock describes. With a
 * five-second TTL that was the ordinary path.
 *
 * The lock was only ever protecting the shared `fresh[]` static. read_console_titles keeps nothing
 * shared - it fills the caller's array and slurps app.db onto the heap - so `fresh` goes on the heap
 * too and the lock is not needed at all. 40 KB per cache miss, at most once every five seconds. */
static int console_titles_cached(ps4_title_t *out, int max) {
    pthread_mutex_lock(&g_titles_lock);
    long long now = now_ms();
    if (g_titles_n < 0 || now - g_titles_at > TITLES_TTL_MS) {
        unsigned gen = g_titles_gen;                  /* what we are answering for */
        pthread_mutex_unlock(&g_titles_lock);
        ps4_title_t *fresh = (ps4_title_t *)calloc(MAX_TITLES, sizeof(ps4_title_t));
        if (!fresh) {
            pthread_mutex_lock(&g_titles_lock);       /* fall through to whatever the cache has */
            int have = g_titles_n > 0 ? g_titles_n : 0;
            if (have > max) have = max;
            if (have > 0 && out) memcpy(out, g_titles, sizeof(ps4_title_t) * (size_t)have);
            pthread_mutex_unlock(&g_titles_lock);
            return have;
        }
        int n = read_console_titles(fresh, MAX_TITLES);
        pthread_mutex_lock(&g_titles_lock);
        /* A DROP WHILE WE WERE READING WINS. Our answer predates it, so storing it would undo an
           invalidation somebody raised on purpose - see g_titles_gen. The caller still gets this
           read's rows; only the cache is left empty so the next question goes to app.db again. */
        if (gen != g_titles_gen) {
            int m = n > 0 ? n : 0;
            if (m > max) m = max;
            if (m > 0 && out) memcpy(out, fresh, sizeof(ps4_title_t) * (size_t)m);
            pthread_mutex_unlock(&g_titles_lock);
            free(fresh);
            return m;
        }
        if (n > 0 || g_titles_n < 0) {
            memcpy(g_titles, fresh, sizeof(ps4_title_t) * (size_t)(n > 0 ? n : 0));
            g_titles_n = n;
            g_titles_at = now;
        }
        free(fresh);
    }
    int n = g_titles_n > 0 ? g_titles_n : 0;
    if (n > max) n = max;
    if (n > 0 && out) memcpy(out, g_titles, sizeof(ps4_title_t) * (size_t)n);
    pthread_mutex_unlock(&g_titles_lock);
    return n;
}

static int console_lists_title(const char *tid) {
    if (!tid || !tid[0]) return 0;
    /* HEAP, NOT A FUNCTION-LOCAL STATIC. A static inside a function is shared by every thread in it,
       and this one is called from the install poller, the stall detector and the routes at the same
       time - so one thread's rows were overwritten while another walked them. 40 KB is too much for a
       thread stack, which is why it was static; the heap is the answer. */
    ps4_title_t *rows = (ps4_title_t *)calloc(MAX_TITLES, sizeof(ps4_title_t));
    if (!rows) return 0;
    int n = console_titles_cached(rows, MAX_TITLES);
    int hit = 0;
    for (int i = 0; i < n; i++)
        if (!strcmp(rows[i].tid, tid)) { hit = 1; break; }
    free(rows);
    return hit;
}

/* The USB scan, remembered briefly. Every /api/library used to walk eight mount points and read
   the header of every package on them; the companion alone asks twice a minute, and the page asks
   too. A stick plugged in shows up within twenty seconds, which nobody notices - the console does.
   Guarded by g_scan_lock, like everything else that touches these buffers. */
static usbpkg_t g_usb[USBPKG_MAX];
static int g_usb_n = 0;
static long long g_usb_at = 0;
#define USB_SCAN_TTL_MS 20000

static int usb_scan_cached(void) {
    long long now = now_ms();
    if (!g_usb_at || now - g_usb_at > USB_SCAN_TTL_MS) {
        g_usb_n = usb_scan(g_usb, USBPKG_MAX);
        g_usb_at = now;
    }
    return g_usb_n;
}

static char *build_library_json_locked(void) {
    static ps4_title_t rows[MAX_TITLES];
    int n = console_titles_cached(rows, MAX_TITLES);

    /* SMALLER RATHER THAN NOTHING. This runs inside a shared system daemon whose heap this payload
       does not own, and after enough hot-reloads a 256 KB request can simply fail - which used to
       turn the whole library into {"ok":false,"error":"out of memory"}, a sentence that sounds like
       the PC ran out when it was the console. Half a library beats none, and the caller is told. */
    size_t cap = 256 * 1024, len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) { cap = 64 * 1024; buf = (char *)malloc(cap); }
    if (!buf) { cap = 16 * 1024; buf = (char *)malloc(cap); }
    if (!buf) return NULL;
    len += (size_t)snprintf(buf + len, cap - len, "{\"ok\":true,\"platform\":\"ps4\",\"games\":[");
    int wrote = 0;
    for (int i = 0; i < n; i++) {
        long long pkg = installed_app_pkg(rows[i].tid);
        char ipath[600];
        int has_icon = icon_path_for(rows[i].tid, ipath, sizeof(ipath));
        char en[400], ec[160];
        json_escape(rows[i].name[0] ? rows[i].name : rows[i].tid, en, sizeof(en));
        json_escape(rows[i].cid, ec, sizeof(ec));
        if (len + 900 >= cap) {
            size_t ncap = cap * 2;
            char *nb = (char *)realloc(buf, ncap);
            if (!nb) break;
            buf = nb; cap = ncap;
        }
        len += (size_t)snprintf(buf + len, cap - len,
            "%s{\"title_id\":\"%s\",\"name\":\"%s\",\"content_id\":\"%s\",\"platform\":\"PS4\","
            "\"size\":%lld,\"installed\":%s,\"installed_version\":\"%s\",\"has_icon\":%s,"
            "\"on_console\":true,\"source\":\"console\"}",
            wrote ? "," : "", rows[i].tid, en, ec,
            rows[i].size, pkg > 0 ? "true" : "false", rows[i].ver, has_icon ? "true" : "false");
        wrote++;
    }

    /* PACKAGES ON A STICK, listed beside the installed games. The companion reads these out of the
       same document (its console_usb_packages filters on `source` starting "usb"), so a PS4 with a
       USB drive full of packages is installable from any device with this PC switched off - which is
       how PS4 owners actually keep their library. `install_key` is "local:<path>", exactly the form
       the PS5 build's /api/install already takes, so one shape covers both consoles. */
    {
        usbpkg_t *usb = g_usb;
        int un = usb_scan_cached();
        for (int i = 0; i < un; i++) {
            char tid[16];
            tid_from_cid(usb[i].cid, tid, sizeof(tid));
            char en[400], ec[160], ep[1400], ef[600];
            json_escape(usb[i].title[0] ? usb[i].title : usb[i].file, en, sizeof(en));
            json_escape(usb[i].cid, ec, sizeof(ec));
            json_escape(usb[i].path, ep, sizeof(ep));
            json_escape(usb[i].file, ef, sizeof(ef));
            if (len + 2600 >= cap) {
                size_t ncap = cap * 2;
                char *nb = (char *)realloc(buf, ncap);
                if (!nb) break;
                buf = nb; cap = ncap;
            }
            /* THE PACKAGE GOES IN THE ARRAY FOR WHAT IT IS. Every one of these used to be
               written into `base` whatever its category said, so a patch on a stick - category
               "gp", which kind_for_category correctly calls "update" - arrived as a full-game
               card with an update inside it, under the game's own name and size. Two cards for
               one game, and pressing the wrong one installs a patch as if it were the game. The
               category has been read correctly all along; only the array was wrong. */
            const char *pk = kind_for_category(usb[i].cat);
            int as_update = !strcmp(pk, "update");
            int as_dlc    = !strcmp(pk, "dlc");
            char row[1500];
            snprintf(row, sizeof(row),
                "{\"install_key\":\"local:%s\",\"file\":\"%s\",\"size\":%lld,"
                "\"kind\":\"%s\",\"content_id\":\"%s\",\"name\":\"%s\","
                "\"title_id\":\"%s\"}",
                ep, ef, usb[i].size, pk, ec, en, tid);
            len += (size_t)snprintf(buf + len, cap - len,
                "%s{\"title_id\":\"%s\",\"name\":\"%s\",\"content_id\":\"%s\","
                "\"platform\":\"PS4\",\"size\":%lld,\"installed\":false,\"has_icon\":false,"
                "\"on_console\":false,\"source\":\"%s\",\"lane\":\"install\","
                "\"local_path\":\"%s\",\"file\":\"%s\",\"kind\":\"%s\","
                "\"base\":[%s],\"updates\":[%s],\"dlc\":[%s],\"cheats\":[]}",
                wrote ? "," : "", tid, en, ec, usb[i].size, usb[i].drive, ep, ef, pk,
                (as_update || as_dlc) ? "" : row,
                as_update ? row : "",
                as_dlc ? row : "");
            wrote++;
        }
    }
    len += (size_t)snprintf(buf + len, cap - len, "],\"count\":%d}", wrote);
    return buf;
}

static char *build_installed_json_locked(void) {
    static ps4_title_t rows[MAX_TITLES];
    int n = console_titles_cached(rows, MAX_TITLES);
    size_t cap = 32 * 1024, len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) return NULL;
    len += (size_t)snprintf(buf + len, cap - len, "{\"ok\":true,\"installed\":[");
    int wrote = 0;
    for (int i = 0; i < n; i++) {
        if (installed_app_pkg(rows[i].tid) <= 0) continue;   /* bytes, not just a folder */
        if (len + 64 >= cap) break;
        len += (size_t)snprintf(buf + len, cap - len, "%s\"%s\"", wrote ? "," : "", rows[i].tid);
        wrote++;
    }
    len += (size_t)snprintf(buf + len, cap - len, "],\"count\":%d}", wrote);
    return buf;
}

/* Drives the console really has. The PS4 has its internal HDD plus usb0..usb6. */
static void send_devices(int fd) {
    char out[2048];
    size_t len = 0;
    len += (size_t)snprintf(out + len, sizeof(out) - len, "{\"ok\":true,\"platform\":\"ps4\",\"ps5\":[");
    struct statvfs vfs;
    int wrote = 0;
    if (statvfs("/user", &vfs) == 0) {
        long long freeb = (long long)vfs.f_bavail * (long long)vfs.f_frsize;
        long long total = (long long)vfs.f_blocks * (long long)vfs.f_frsize;
        len += (size_t)snprintf(out + len, sizeof(out) - len,
                                "{\"id\":\"internal\",\"label\":\"Internal HDD\",\"free\":%lld,"
                                "\"total\":%lld,\"detected\":true}", freeb, total);
        wrote++;
    }
    for (int i = 0; i < 7; i++) {
        char p[32];
        snprintf(p, sizeof(p), "/mnt/usb%d", i);
        struct stat st;
        int detected = 0;
        long long freeb = 0, total = 0;
        if (stat(p, &st) == 0 && S_ISDIR(st.st_mode) && statvfs(p, &vfs) == 0 && vfs.f_blocks > 0) {
            detected = 1;
            freeb = (long long)vfs.f_bavail * (long long)vfs.f_frsize;
            total = (long long)vfs.f_blocks * (long long)vfs.f_frsize;
        }
        if (len + 200 >= sizeof(out)) break;
        if (detected)
            len += (size_t)snprintf(out + len, sizeof(out) - len,
                                    "%s{\"id\":\"usb%d\",\"label\":\"USB%d\",\"free\":%lld,"
                                    "\"total\":%lld,\"detected\":true}",
                                    wrote ? "," : "", i, i, freeb, total);
        else
            len += (size_t)snprintf(out + len, sizeof(out) - len,
                                    "%s{\"id\":\"usb%d\",\"label\":\"USB%d\",\"detected\":false}",
                                    wrote ? "," : "", i, i);
        wrote++;
    }
    snprintf(out + len, sizeof(out) - len, "]}");
    send_json(fd, out);
}

/* The three wrappers, and they are the ONLY callers of the _locked builders (checked: one caller
   each). g_scan_lock is what makes the builders' function-local `static ps4_title_t rows[MAX_TITLES]`
   and the shared g_usb buffers safe, so a caller that skipped a wrapper would take that away silently.
   console_titles_cached no longer takes this lock itself - it used to, from inside these builders,
   which handed the critical section away mid-scan. */
static char *build_library_json(void) {
    pthread_mutex_lock(&g_scan_lock);
    char *r = build_library_json_locked();
    pthread_mutex_unlock(&g_scan_lock);
    return r;
}

static char *build_installed_json(void) {
    pthread_mutex_lock(&g_scan_lock);
    char *r = build_installed_json_locked();
    pthread_mutex_unlock(&g_scan_lock);
    return r;
}

static void storage_json(char *out, size_t outsz) {
    static ps4_title_t rows[MAX_TITLES];
    int n = console_titles_cached(rows, MAX_TITLES);
    long long games = 0;
    int count = 0;
    for (int i = 0; i < n; i++)
        if (installed_app_pkg(rows[i].tid) > 0) { games += rows[i].size; count++; }
    struct statvfs vfs;
    long long freeb = 0, total = 0;
    if (statvfs("/user", &vfs) == 0) {
        freeb = (long long)vfs.f_bavail * (long long)vfs.f_frsize;
        total = (long long)vfs.f_blocks * (long long)vfs.f_frsize;
    }
    snprintf(out, outsz,
             "{\"ok\":true,\"reachable\":true,\"drives\":[{\"id\":\"internal\","
             "\"label\":\"Internal HDD\",\"used\":%lld,\"games_bytes\":%lld,\"free\":%lld,"
             "\"total\":%lld,\"count\":%d,\"kind\":\"console\"}]}",
             total - freeb, games, freeb, total, count);
}

static void send_storage(int fd) {
    /* Built under the lock, sent outside it: a socket write can block on a peer that has stopped
       reading, and holding the scan lock through that would stall every other request. */
    char out[600];
    pthread_mutex_lock(&g_scan_lock);
    storage_json(out, sizeof(out));
    pthread_mutex_unlock(&g_scan_lock);
    send_json(fd, out);
}

/* --------------------------------------------------------------------- routes */

static volatile int g_quit = 0;
/* The listening socket, so /api/quit can break the accept loop out of its blocking wait without
   killing the process we are injected into. -1 once it has been handed over. */
static volatile int g_srv = -1;

/* ------------------------------------------- serving a local package to the installer
 *
 * GET/HEAD /pkgfile/<token>, with byte ranges, because the installer asks for them: it HEADs first
 * for the size, then pulls the package in pieces and re-requests from where it left off. A 200-only
 * server looks like it works and then fails partway through a large install.
 */
static void serve_pkgfile(int fd, const char *path, const char *req, int head_only) {
    char tok[24] = {0};
    const char *t = path + 9;                       /* after "/pkgfile/" */
    size_t k = 0;
    /* STOP AT THE DOT TOO. install_local_pkg registers the url /pkgfile/<n>.pkg - the extension
       is there because the console refuses a package url without one - and this loop stopped only
       at '?' and '/', so the token became "0.pkg", the digits test below failed on the '.', and
       this server answered its OWN url with 404. Every console-local install died there: a USB
       package, /api/engine/install-local, and the dashboard app installing itself.

       It is in the console's own log, and it was misread for a long time as a network problem:
           [BGFT] ERROR: [360] status = 404
           install: register failed rc=0x80991404 ... uri=http://10.0.0.86:8710/pkgfile/0.pkg
       0x80991404 was recorded as "who serves it, not loopback-vs-LAN". It was neither - it was
       this parser. The 404 in that line is ours.

       The PS5 twin has always done this correctly (ps5-app/onconsole/server.c, "const char *dot =
       strchr(path + 1, '.')"), which is the shape copied here. */
    while (t[k] && t[k] != '?' && t[k] != '/' && t[k] != '.' && k + 1 < sizeof(tok)) { tok[k] = t[k]; k++; }
    tok[k] = 0;
    char local[1024];
    /* DIGITS ONLY. atoi() answers 0 for anything it cannot parse, so /pkgfile/anything served
       whatever package happened to be token 0 - to any caller, and the installer is not the only
       thing that can ask. */
    int all_digits = 1;
    for (const char *q = tok; *q; q++)
        if (*q < '0' || *q > '9') { all_digits = 0; break; }
    if (!tok[0] || !all_digits || !localpkg_path(atoi(tok), local, sizeof(local))) {
        send_status(fd, "404 Not Found", "text/plain", "no such package");
        return;
    }
    int f = open(local, O_RDONLY);
    struct stat st;
    if (f < 0 || fstat(f, &st) != 0 || !S_ISREG(st.st_mode)) {
        if (f >= 0) close(f);
        send_status(fd, "404 Not Found", "text/plain", "the package is gone");
        return;
    }
    long long size = (long long)st.st_size, start = 0, end = size - 1;
    int partial = 0;
    const char *rh = strcasestr_local(req, "range:");
    if (rh) {
        const char *eq = strchr(rh, '=');
        if (eq) {
            eq++;
            while (*eq == ' ') eq++;
            if (*eq == '-') {                        /* suffix range: the last N bytes */
                long long n = atoll(eq + 1);
                if (n > 0) { start = size - n; if (start < 0) start = 0; }
            } else {
                start = atoll(eq);
                const char *dash = strchr(eq, '-');
                if (dash && dash[1] >= '0' && dash[1] <= '9') {
                    long long e2 = atoll(dash + 1);
                    if (e2 < end) end = e2;
                }
            }
            partial = 1;
        }
    }
    if (start > end || start >= size) {
        char h[200];
        int hn = snprintf(h, sizeof(h),
                          "HTTP/1.1 416 Range Not Satisfiable\r\nContent-Range: bytes */%lld\r\n"
                          "Content-Length: 0\r\nConnection: close\r\n\r\n", size);
        write_all(fd, h, (size_t)hn);
        close(f);
        return;
    }
    long long length = end - start + 1;
    char hdr[420];
    int hn;
    if (partial)
        hn = snprintf(hdr, sizeof(hdr),
                      "HTTP/1.1 206 Partial Content\r\nContent-Type: application/octet-stream\r\n"
                      "Accept-Ranges: bytes\r\nContent-Length: %lld\r\n"
                      "Content-Range: bytes %lld-%lld/%lld\r\nConnection: close\r\n\r\n",
                      length, start, end, size);
    else
        hn = snprintf(hdr, sizeof(hdr),
                      "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                      "Accept-Ranges: bytes\r\nContent-Length: %lld\r\nConnection: close\r\n\r\n",
                      length);
    write_all(fd, hdr, (size_t)hn);
    if (head_only) { close(f); return; }
    /* THE SEND TIMEOUT COMES OFF. conn_thread arms a 60 s SO_SNDTIMEO so a dead client cannot hold
       a thread forever, which is right for an API answer and wrong for this: the installer stops
       reading for as long as it takes to promote what it already has, and a write that times out
       part-way through is an install that fails for no reason the user can see. */
    struct timeval none;
    none.tv_sec = 0; none.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &none, sizeof(none));
    if (lseek(f, (off_t)start, SEEK_SET) != (off_t)start) { close(f); return; }
    char *chunk = (char *)malloc(256 * 1024);
    if (!chunk) { close(f); return; }
    long long left = length;
    while (left > 0) {
        size_t want = left > (long long)(256 * 1024) ? (size_t)(256 * 1024) : (size_t)left;
        ssize_t r = read(f, chunk, want);
        if (r <= 0) break;
        /* Stop when the reader goes away. With SIGPIPE ignored this is a plain EPIPE, and carrying
           on would spend the rest of the file writing into a socket nobody is holding. */
        if (write_all(fd, chunk, (size_t)r) != 0) break;
        left -= r;
    }
    free(chunk);
    close(f);
}

/* One string field out of a small JSON body. The shop's own page and the companion are the only
   callers, so this stays a reader rather than a parser: it finds "key" and takes the quoted value
   after the colon. */
static void json_str_field(const char *body, const char *key, char *out, size_t outsz) {
    out[0] = 0;
    if (!body || !key) return;
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *k = strstr(body, pat);
    if (!k) return;
    const char *c = strchr(k + strlen(pat), ':');
    if (!c) return;
    const char *s1 = strchr(c, '"');
    if (!s1) return;
    s1++;
    size_t j = 0;
    while (*s1 && *s1 != '"' && j + 1 < outsz) {
        if (*s1 == '\\' && s1[1]) s1++;
        out[j++] = *s1++;
    }
    out[j] = 0;
}

/* One NUMBER out of the same small JSON body. json_str_field only reads quoted values, and the
   fields BGFT actually needs - size above all - arrive unquoted. There was no reader for them, so
   the on-console install route passed 0 and the console refused the task. Returns 0 when absent,
   which is what the callers already treat as "not stated". */
static long long json_num_field(const char *body, const char *key) {
    if (!body || !key) return 0;
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *k = strstr(body, pat);
    if (!k) return 0;
    const char *c = strchr(k + strlen(pat), ':');
    if (!c) return 0;
    c++;
    while (*c == ' ' || *c == '\t' || *c == '"') c++;
    long long v = 0;
    int any = 0;
    while (*c >= '0' && *c <= '9') { v = v * 10 + (*c - '0'); c++; any = 1; }
    return any ? v : 0;
}

/* Install a package that is already on this console. Returns nothing - it answers `fd` itself. */
/* Install a package that is ALREADY on this console. Returns 0 when the console has taken it;
   `err` carries a sentence either way. Split out from the route below so the ELF's own dashboard-app
   install can use the identical lane - one install path, one set of failures, one place to fix. */
static int install_local_pkg(const char *local, char *err, size_t errsz) {
    struct stat st;
    if (stat(local, &st) != 0 || !S_ISREG(st.st_mode)) {
        snprintf(err, errsz, "That package is not on the console any more");
        return -1;
    }
    char cid[64], cat[16], title[160];
    long long size = 0;
    if (!pkg_file_facts(local, cid, sizeof(cid), cat, sizeof(cat), title, sizeof(title), &size)) {
        snprintf(err, errsz, "That file is not a PS4 package");
        return -2;
    }
    if (!cid[0]) {
        snprintf(err, errsz, "That package does not carry a content id, so the console will not "
                             "accept it");
        return -3;
    }
    job_refresh();
    /* TEST AND CLAIM UNDER ONE LOCK - see the claim block beside g_job. */
    long long claim = job_claim(JOB_CLAIM_FRESH, NULL);
    if (!claim) {
        snprintf(err, errsz, "An install is already running on this PS4");
        return -4;
    }
    int tok = localpkg_register(local);
    if (tok < 0) {
        job_claim_abort(claim);
        snprintf(err, errsz, "Too many packages are already queued from this console - reload the "
                             "shop and try again");
        return -5;
    }
    /* THE URL HAS TO END IN .pkg, and that cost an afternoon to find. Handing the transfer service
       `.../pkgfile/0` is refused with 0x80990033 no matter what address it is on - the console's
       own log says it in words: "[BGFT] ERROR: [2239] Not supported extension." It reads the
       EXTENSION out of the URL and will not touch a package whose url does not look like one. The
       byte-identical file offered from the PC as `/library/NAME.pkg` installed first time, which is
       what made the address look like the culprit when it never was.

       The token parser on the serving side stops at the dot, so `/pkgfile/0.pkg` is still token 0.
       Loopback rather than our LAN address: both are accepted now, and 127.0.0.1 cannot break if
       the console's address changes under us. */
    /* THE CONSOLE'S OWN LAN ADDRESS, NOT 127.0.0.1, AND THE DIFFERENCE IS NOT COSMETIC.
     *
     * With PlayStation Network unreachable - which is the configuration this shop asks for, because a
     * reachable PSN refuses to install any title the Store has an update for and deletes fake-signed
     * retail titles on restart - the download service refuses a LOOPBACK url outright:
     *
     *     uri=http://127.0.0.1:8710/pkgfile/0.pkg   register failed rc=0x80991404
     *     klog: [BGFT] ERROR: [360] status = 404 / [BGFT] [577] !!! CDN Auth Expired !!!
     *
     * The identical package offered at a LAN address registered first time and installed completely,
     * with app.pbm, app.json and app.xml written and a launch ticket created. Two attempts, one
     * difference. Whatever the service does with a loopback host, it ends in an authentication path
     * that cannot work with Sony unreachable.
     *
     * lan_ip_str() is still THIS console serving THIS package to itself - no PC involved, so the app
     * still installs itself with everything else switched off. It just says where it is by its
     * address on the network instead of by loopback. */
    char uri[256];
    snprintf(uri, sizeof(uri), "http://%s:%d/pkgfile/%d.pkg", lan_ip_str(), (int)PORT, tok);
    const char *label = title[0] ? title : local;
    OrbisBgftTaskId task = BGFT_INVALID_TASK_ID;
    if (bgft_install_url(uri, label, cid, size, pkg_type_for_category(cat), err, errsz, &task) != 0) {
        job_claim_abort(claim);
        return -6;
    }

    pthread_mutex_lock(&g_job_lock);
    /* THE CLAIM CAN BE TAKEN WHILE THE REGISTRATION IS IN FLIGHT - a cancel arriving in that window
       clears it. Filling a slot somebody else now owns would orphan a task, which is the whole
       thing this change exists to stop, so the task just made is handed straight back instead. */
    if (!job_claim_is_mine_locked(claim)) {
        pthread_mutex_unlock(&g_job_lock);
        bgft_release(task);
        snprintf(err, errsz, "That install was stopped before it started");
        return -7;
    }
    memset(&g_job, 0, sizeof(g_job));
    g_job.active = 1;
    g_job.task = task;
    g_job.job_id = now_ms();
    g_job.started_ms = now_ms();
    g_job.expect = size;
    snprintf(g_job.uri, sizeof(g_job.uri), "%s", uri);
    snprintf(g_job.name, sizeof(g_job.name), "%s", label);
    snprintf(g_job.cid, sizeof(g_job.cid), "%s", cid);
    snprintf(g_job.cat, sizeof(g_job.cat), "%s", cat ? cat : "");
    tid_from_cid(cid, g_job.tid, sizeof(g_job.tid));
    job_baseline_locked();
    snprintf(g_job.state, sizeof(g_job.state), "downloading");
    snprintf(g_job.msg, sizeof(g_job.msg), "The PS4 is installing it from its own storage");
    g_job_claim = 0;
    pthread_mutex_unlock(&g_job_lock);
    ilog("install: local package %s (%s, %s, %lld bytes) -> task %d",
         local, cid, pkg_type_for_category(cat), size, (int)task);
    return 0;
}

/* ---------------------------------------------------------------- the direct lane
 *
 * Install a package that is ALREADY on this console's storage, using the console's own installer
 * rather than its downloader. No task, no URL, no transfer, and no patch check.
 *
 * Reports through the same g_job as every other install so the page and the companion need to know
 * nothing new; g_job.direct tells job_refresh to read progress from AppInstUtil instead of BGFT.
 */
static void *install_direct_thread(void *arg);

/* THE PATH WE WRITE AND THE PATH THE INSTALLER MUST BE GIVEN ARE NOT THE SAME PATH.
 *
 * Our process sees the shop's own storage as /data. SceShellCore, which is what actually performs
 * the install, sees that identical filesystem as /user/data. Hand it our view and it cannot find
 * the file - measured here as rc=0x80020012 (errno 18) for a package that was demonstrably present
 * and readable, whose title id the console had just read out of it successfully.
 *
 * The PS5 half of this project learned the same lesson and wrote it down (ps5-app/onconsole/server.c,
 * TILE_PKG_INSTALL): there the wrong path answers 0x80A40029. Different console, different code,
 * same trap - which is why this is a named function with the reason attached rather than a string
 * concatenation somewhere in the middle of an install.
 *
 * Anything that is not under /data is passed through untouched: /user/... is already the installer's
 * own view, and a package on USB is somewhere only it knows how to name. */
static void installer_path(const char *ours, char *out, size_t outsz) {
    if (!strncmp(ours, "/data/", 6))
        snprintf(out, outsz, "/user/data/%s", ours + 6);
    else
        snprintf(out, outsz, "%s", ours);
}

static int install_direct_start(const char *local, char *err, size_t errsz) {
    if (!ai_install_pkg_fn) {
        snprintf(err, errsz, "This PS4 does not have the direct installer this needs");
        return -1;
    }
    struct stat st;
    if (stat(local, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size < 4096) {
        snprintf(err, errsz, "There is no package at that path on the console");
        return -2;
    }
    /* A PS4 package begins \x7FCNT. Checking here means a truncated or half-copied file is caught
       before the console's installer is asked to look at it. */
    int f = open(local, O_RDONLY);
    if (f < 0) { snprintf(err, errsz, "The console cannot read that file"); return -3; }
    unsigned char magic[4] = {0};
    ssize_t got = read(f, magic, 4);
    close(f);
    if (got != 4 || magic[0] != 0x7F || magic[1] != 'C' || magic[2] != 'N' || magic[3] != 'T') {
        snprintf(err, errsz, "That file is not a PS4 package");
        return -4;
    }

    /* THE TITLE ID COMES FROM THE CONSOLE, not from our own parsing of the name or the header. */
    char tid[32] = {0};
    int is_app = 0;
    if (ai_tid_from_pkg_fn) {
        int rc = ai_tid_from_pkg_fn(local, tid, &is_app);
        if (rc != 0) {
            ilog("direct: the console could not read a title id from %s (rc=0x%08X)",
                 local, (unsigned)rc);
            tid[0] = 0;
        }
    }

    /* Same staleness guard as the other lanes: a job that finished but has not been polled still
       reads "downloading", and refusing on that would block every install after the first. */
    job_refresh();
    /* Claimed the same way as every other lane, and for the same reason - the gap here is file I/O
       rather than a service call, which is wider, not narrower. */
    long long dclaim = job_claim(JOB_CLAIM_DIRECT, NULL);
    if (!dclaim) {
        snprintf(err, errsz, "An install is already running on this PS4 - wait for it to finish");
        return -5;
    }

    /* THE CONTENT ID, for progress. sceAppInstUtilGetInstallProgressInfo is keyed on the content id,
       not the title id, and pkg_file_facts already reads it straight out of the package header - the
       same reader the BGFT lane uses. Progress is a nicety; the verdict never depends on it. */
    char cid[64] = {0}, cat[16] = {0}, title[160] = {0};
    long long psize = 0;
    pkg_file_facts(local, cid, sizeof(cid), cat, sizeof(cat), title, sizeof(title), &psize);

    /* AN UPDATE IS NOT A BASE GAME, and this lane will hand either one to the console without an
       opinion unless it is told to have one.
       Measured the hard way: a 10,682,368-byte UPDATE package for a game that was not installed was
       accepted and written to /user/app/CUSA14409/app.pkg - the base-game slot - with artwork under
       /user/appmeta and NO row in app.db. Two orphaned directories and a title the console did not
       list. The package's own param.sfo says what it is ('gd' a game, 'gp' a patch, 'ac' additional
       content), and the rule is the same one the PC companion already enforces: an add-on needs its
       base game present, because the console has nowhere to put it otherwise. */
    if (!strcmp(cat, "gp") || !strcmp(cat, "ac")) {
        char btid[32] = {0};
        if (tid[0]) snprintf(btid, sizeof(btid), "%s", tid);
        else        tid_from_cid(cid, btid, sizeof(btid));
        /* NOT sceAppInstUtilAppExists. It answered "exists" for a title whose /user/app directory
           had just been deleted by hand and which app.db had never listed - so whatever question it
           answers, it is not "is this game installed". Measured: the gate below passed on its word
           and a 10 MB update was written into the base-game slot for the second time.
           The two proofs this project already trusts are used instead, and both must agree: a row in
           the console's own database, and app.pkg on disk with bytes in it. */
        int listed = btid[0] && console_lists_title(btid);
        long long base_bytes = btid[0] ? installed_app_pkg(btid) : 0;
        if (!listed || base_bytes <= 0) {
            snprintf(err, errsz,
                     "This is %s, and %s is not installed on this PS4 yet. Install the game first, "
                     "then add this",
                     !strcmp(cat, "gp") ? "an update" : "extra content",
                     title[0] ? title : "the game it belongs to");
            ilog("direct: refused %s - category '%s' for %s, which the console %s and whose "
                 "app.pkg is %lld bytes", local, cat, btid[0] ? btid : "?",
                 listed ? "lists" : "does not list", base_bytes);
            job_claim_abort(dclaim);
            return -7;
        }
    }

    pthread_mutex_lock(&g_job_lock);
    if (!job_claim_is_mine_locked(dclaim)) {   /* taken from us - see install_local_pkg */
        pthread_mutex_unlock(&g_job_lock);
        snprintf(err, errsz, "That install was stopped before it started");
        return -8;
    }
    memset(&g_job, 0, sizeof(g_job));
    g_job.active = 1;
    g_job.direct = 1;
    g_job.task = BGFT_INVALID_TASK_ID;
    g_job.job_id = now_ms();
    g_job.started_ms = now_ms();
    g_job.expect = (long long)st.st_size;
    snprintf(g_job.uri, sizeof(g_job.uri), "%s", local);
    snprintf(g_job.cid, sizeof(g_job.cid), "%s", cid);
    snprintf(g_job.cat, sizeof(g_job.cat), "%s", cat);
    /* The console's own title id wins; the content id's is the fallback. */
    if (tid[0]) snprintf(g_job.tid, sizeof(g_job.tid), "%s", tid);
    else        tid_from_cid(cid, g_job.tid, sizeof(g_job.tid));
    const char *base = strrchr(local, '/');
    snprintf(g_job.name, sizeof(g_job.name), "%s", title[0] ? title : (base ? base + 1 : local));
    job_baseline_locked();
    snprintf(g_job.state, sizeof(g_job.state), "downloading");
    snprintf(g_job.msg, sizeof(g_job.msg), "The PS4 is installing it from its own storage");
    g_job_claim = 0;
    pthread_mutex_unlock(&g_job_lock);

    char ipath[1100];
    installer_path(local, ipath, sizeof(ipath));
    ilog("direct: installing %s (%lld bytes, title %s, category '%s') with the console's own "
         "installer%s%s", local, (long long)st.st_size, tid[0] ? tid : "?", cat[0] ? cat : "?",
         strcmp(ipath, local) ? " - it sees that file as " : "",
         strcmp(ipath, local) ? ipath : "");

    pthread_t t;
    pthread_attr_t at;
    pthread_attr_init(&at);
    pthread_attr_setstacksize(&at, 256 * 1024);
    char *copy = strdup(ipath);
    if (!copy || pthread_create(&t, &at, install_direct_thread, copy) != 0) {
        free(copy);
        pthread_attr_destroy(&at);
        pthread_mutex_lock(&g_job_lock);
        g_job.active = 0;
        pthread_mutex_unlock(&g_job_lock);
        snprintf(err, errsz, "The console could not start the install");
        return -6;
    }
    pthread_detach(t);
    pthread_attr_destroy(&at);
    return 0;
}

static void *install_direct_thread(void *arg) {
    char *local = (char *)arg;
    char tid[32];
    pthread_mutex_lock(&g_job_lock);
    snprintf(tid, sizeof(tid), "%s", g_job.tid);
    pthread_mutex_unlock(&g_job_lock);

    /* AN EXISTING TITLE HAS TO BE PREPARED FOR OVERWRITE, which is the console's own word for it.
       Skipped when we could not learn the title id - the installer is then left to decide. */
    if (tid[0] && ai_prep_overwrite_fn && installed_app_pkg(tid) > 0 && console_lists_title(tid)) {
        int rc = ai_prep_overwrite_fn(local);
        ilog("direct: %s is already installed - prepare-overwrite rc=0x%08X", tid, (unsigned)rc);
    }

    int rc = ai_install_pkg_fn(local, NULL);
    ilog("direct: the console's installer answered rc=0x%08X for %s", (unsigned)rc, local);

    /* THE VERDICT IS job_refresh'S TO WRITE, NOT THIS THREAD'S. Both of them writing it raced: a
       refresh that read the return code as zero (because this thread had not stored it yet) then
       wrote "downloading" over the failure this thread had just recorded, and the job reported a
       non-zero code beside a running state for ever. So this stores the code and nothing else, and
       one function turns codes into verdicts - which is how the BGFT lane has always worked. */
    pthread_mutex_lock(&g_job_lock);
    g_job.rc = (unsigned)rc;
    pthread_mutex_unlock(&g_job_lock);
    free(local);
    return NULL;
}

static void install_local_path(int fd, const char *local) {
    char err[320] = {0};
    if (install_local_pkg(local, err, sizeof(err)) != 0) {
        int busy = (strstr(err, "already running") != NULL);
        char esc[400], out[620];
        json_escape(err, esc, sizeof(esc));
        snprintf(out, sizeof(out), "{\"ok\":false,\"queued\":false,%s\"error\":\"%s\"}",
                 busy ? "\"busy\":true," : "", esc);
        if (busy) send_status(fd, "409 Conflict", "application/json", out);
        else      send_json(fd, out);
        return;
    }
    pthread_mutex_lock(&g_job_lock);
    long long jid = g_job.job_id;
    int task = (int)g_job.task;
    pthread_mutex_unlock(&g_job_lock);
    char out[260];
    snprintf(out, sizeof(out),
             "{\"ok\":true,\"queued\":true,\"local\":true,\"job_id\":%lld,\"task\":%d}",
             jid, task);
    send_json(fd, out);
}

#ifndef PMS_LITE
/* ------------------------------------------------ the dashboard app this ELF carries
 *
 * The PS5 build installs its tile on boot; this does the same for the PS4, and for the same reason:
 * without it there is nothing on the console to press, and the shop only exists while this payload
 * happens to be loaded.
 *
 * INTELLIGENTLY, meaning it does the least it can get away with:
 *   - already installed at this version or newer  -> nothing at all, not even a write to disk
 *   - installed but older                         -> install over it, which is how the PS4 updates
 *   - not there                                   -> install it
 *
 * "Installed" is app.pkg on disk with bytes in it, never an app.db row on its own - the rule this
 * project learned on the PS5, where trusting the row produced 53 phantom installs. The version
 * comes from tbl_appinfo's APP_VER, which is the field the console itself updates when a package
 * is installed over another.
 *
 * Nothing here touches the jailbreak's folders or files. The staged copy lives under our own
 * /data/pkg-mutant-shop, and the install goes down our own BGFT lane - the same one a game from
 * the PC uses, serving the file to the console from this very process.
 */
/* THE VERSION THE ELF CARRIES. It must be HIGHER than what the console already has or the check
   below decides there is nothing to do - and "01.00" here against the package's "1.00" normalises
   to the same number, so every fix shipped in the package would have been invisible for ever.
   ps4-app/onconsole/build-wsl.sh now refuses to build when these two disagree. Bump BOTH whenever
   ps4-app/tile-pkg changes. Zero-padded NN.NN, which is the form every other title on the console
   uses. */
#define PS4_TILE_VER   "01.04"
#define TILE_PKG_DISK  SHOP_DATA_DIR "/pms-tile.pkg"

/* "01.02" -> 102, "1.00" -> 100. Format-tolerant on purpose: the console stores whatever the
   package's param.sfo carried, and a leading zero must not make 01.00 look older than 1.00. */
static int tile_ver_num(const char *v) {
    int n = 0, seen = 0;
    for (const char *p = v ? v : ""; *p; p++) {
        if (*p >= '0' && *p <= '9') { n = n * 10 + (*p - '0'); seen = 1; }
        else if (*p != '.') break;
    }
    return seen ? n : -1;
}

/* The version of the app the console currently has, or "" if it has none. */
static void tile_installed_ver(char *out, size_t outsz) {
    out[0] = 0;
    /* No g_scan_lock and no shared static: console_titles_cached needs neither now, and taking that
       lock here was half of the recursive-lock defect it used to be on the other side of. */
    ps4_title_t *rows = (ps4_title_t *)calloc(MAX_TITLES, sizeof(ps4_title_t));
    if (!rows) return;
    int n = console_titles_cached(rows, MAX_TITLES);
    for (int i = 0; i < n; i++)
        if (!strcmp(rows[i].tid, PS4_TILE_TID)) { snprintf(out, outsz, "%s", rows[i].ver); break; }
    free(rows);
}

/* Write the embedded package out and hand it to the install lane. 0 = handed over. */
static int tile_stage_and_install(char *detail, size_t dsz) {
    size_t len = (size_t)(tb_ps4_tile_pkg_end - tb_ps4_tile_pkg);
    /* A PS4 package begins \x7FCNT. Checking it here means a truncated or mis-built bundle is
       caught before the console is asked to install anything. */
    if (len < 4096 || tb_ps4_tile_pkg[0] != 0x7F || tb_ps4_tile_pkg[1] != 'C' ||
        tb_ps4_tile_pkg[2] != 'N' || tb_ps4_tile_pkg[3] != 'T') {
        snprintf(detail, dsz, "the embedded package looks wrong (%zu bytes)", len);
        return -1;
    }
    mkdir(SHOP_DATA_DIR, 0777);
    int f = open(TILE_PKG_DISK, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (f < 0) { snprintf(detail, dsz, "cannot write %s", TILE_PKG_DISK); return -2; }
    size_t off = 0;
    while (off < len) {
        ssize_t w = write(f, tb_ps4_tile_pkg + off, len - off);
        if (w <= 0) break;
        off += (size_t)w;
    }
    close(f);
    if (off != len) { snprintf(detail, dsz, "short write %zu/%zu", off, len); return -3; }

    /* TWO LANES, AND THE SECOND ONE IS NOT A CONSOLATION PRIZE.
     *
     * The BGFT lane is preferred because a finished BGFT task is what makes the icon openable at all.
     * But BGFT refuses to register anything when it cannot authenticate with Sony's content network -
     * measured as 0x80991404, klog "!!! CDN Auth Expired !!!" - and a console with PlayStation Network
     * blocked is exactly the configuration this shop asks for, because a reachable PSN refuses to
     * install any title the Store has an update for and deletes fake-signed retail titles on restart.
     *
     * So when BGFT will not take it, the console's own installer will: the app is installed and up to
     * date either way. What the direct lane cannot produce is the launch ticket, and that is said out
     * loud rather than papered over - an icon that is present but will not open is a different problem
     * from an icon that is missing, and the log should let someone tell them apart. */
    char err[256] = {0};
    if (install_local_pkg(TILE_PKG_DISK, err, sizeof(err)) == 0) {
        snprintf(detail, dsz, "handed over to the download service (%zu bytes)", len);
        return 0;
    }
    /* THE FALLBACK MUST NOT MAKE THINGS WORSE, and it did.
     *
     * The console's own installer writes app.pkg and extracts the artwork, but NOT app.pbm, app.json
     * or app.xml, and it creates no launch ticket. Measured: it turned a COMPLETE five-file install of
     * this app into app.pkg alone, and the icon went from opening the shop to CE-32930-7. That is a
     * downgrade dressed up as a repair.
     *
     * So it is only used when there is nothing to lose - the app is not installed, or it is installed
     * but already unlaunchable. An install that is complete and has a ticket is left exactly as it is,
     * and the reason is logged, because "the shop declined to touch a working icon" is information and
     * silence is not. The PC repairs it properly when it can (see the companion's tile watchdog):
     * BGFT accepts a package served by the PC even when it refuses one this console serves itself. */
    long long have_bytes = installed_app_pkg(PS4_TILE_TID);
    int have_ticket = ticket_count_for(PS4_TILE_TID) > 0;
    if (have_bytes > 0 && have_ticket) {
        ilog("tile: the download service would not take it (%s) - and the icon on this console is "
             "already installed and openable, so it is being left alone rather than replaced by an "
             "install that cannot make a launch ticket", err);
        snprintf(detail, dsz, "%s - left the working icon alone", err);
        return -5;
    }
    ilog("tile: the download service would not take it (%s) - trying the console's own installer "
         "(it cannot create a launch ticket, so the icon may need the PC to finish the job)", err);
    char err2[256] = {0};
    if (install_direct_start(TILE_PKG_DISK, err2, sizeof(err2)) == 0) {
        snprintf(detail, dsz, "handed to the console's own installer (%zu bytes) - note that this "
                              "lane cannot create a launch ticket", len);
        return 0;
    }
    snprintf(detail, dsz, "%s, and the console's own installer said: %s", err, err2);
    return -4;
}

/* IS THE CONSOLE'S COPY THE PACKAGE THIS ELF CARRIES?
 *
 * The version number alone cannot answer that, and this console proved it: a cancelled update left
 * app.db saying 01.02 while /user/app/PKGM00001/app.pkg still held the 01.01 bytes. A version check
 * on its own then says "nothing to do" for ever and the console stays wrong permanently.
 *
 * So compare the bytes. One sequential read of the installed package against the copy already in
 * this process's memory, 64 KB at a time, no allocation, stopping at the first difference - which
 * for a mismatch is usually inside the first block. Measured twice on 13.52: the installed app.pkg
 * is byte-identical to the package we hand over, so equality is the right expectation.
 * Returns 1 when they match, and 1 when the file cannot be read through - an unreadable file is not
 * evidence of a mismatch, and guessing would reinstall on every boot. */
static int tile_bytes_match(void) {
    size_t len = (size_t)(tb_ps4_tile_pkg_end - tb_ps4_tile_pkg);
    long long sz = 0, mt = 0;
    if (!app_pkg_facts(PS4_TILE_TID, &sz, &mt)) return 1;
    if ((size_t)sz != len) return 0;
    for (int i = 0; APP_ROOTS[i]; i++) {
        char p[600];
        snprintf(p, sizeof(p), "%s/%s/app.pkg", APP_ROOTS[i], PS4_TILE_TID);
        int f = open(p, O_RDONLY);
        if (f < 0) continue;
        unsigned char buf[65536];
        size_t off = 0;
        int same = 1;
        while (off < len) {
            size_t want = len - off < sizeof(buf) ? len - off : sizeof(buf);
            ssize_t r = read(f, buf, want);
            if (r <= 0) { same = -1; break; }
            if (memcmp(buf, tb_ps4_tile_pkg + off, (size_t)r)) { same = 0; break; }
            off += (size_t)r;
        }
        close(f);
        if (same < 0) return 1;      /* could not finish reading - do not call that a mismatch */
        return same;
    }
    return 1;
}

/* ONE REPAIR ATTEMPT PER BUILD. If some future firmware re-wraps app.pkg as it installs it, the
 * compare above would differ on every boot and this shop would reinstall its own icon on every
 * boot. The stamp records which build already tried, so that costs one install, once.
 *
 * IT HAS TO IDENTIFY THE BUILD, NOT THE VERSION NUMBER. The first version of this wrote
 * PS4_TILE_VER alone, and that is not enough: the package changes whenever anything inside it
 * changes - the lite payload it carries, a rebuilt eboot - while the version stays put for a fix
 * that does not deserve a bump. Measured immediately: a rebuilt 01.04 package was refused with
 * "still differs after one repair" and the console kept the older 01.04 bytes for ever.
 *
 * So the stamp is the version plus a fingerprint of the package this ELF actually carries. A new
 * build is a new fingerprint and gets its one attempt; the same build hitting the same mismatch
 * twice is the runaway case the stamp exists to stop, and still stops. FNV-1a over a copy already
 * in memory - no file read, and it runs once at boot. */
#define TILE_STAMP SHOP_DATA_DIR "/tile-repair.stamp"

static void tile_build_id(char *out, size_t outsz) {
    size_t len = (size_t)(tb_ps4_tile_pkg_end - tb_ps4_tile_pkg);
    unsigned long long h = 1469598103934665603ULL;          /* FNV-1a 64 offset basis */
    for (size_t i = 0; i < len; i++) {
        h ^= (unsigned long long)tb_ps4_tile_pkg[i];
        h *= 1099511628211ULL;
    }
    snprintf(out, outsz, "%s-%zu-%016llx", PS4_TILE_VER, len, h);
}

static int tile_repair_tried(void) {
    char want[64];
    tile_build_id(want, sizeof(want));
    int f = open(TILE_STAMP, O_RDONLY);
    if (f < 0) return 0;
    char b[64] = {0};
    ssize_t n = read(f, b, sizeof(b) - 1);
    close(f);
    if (n <= 0) return 0;
    for (int i = 0; b[i]; i++)
        if (b[i] == '\n' || b[i] == '\r') { b[i] = 0; break; }
    return !strcmp(b, want);
}
static void tile_repair_mark(void) {
    char id[64];
    tile_build_id(id, sizeof(id));
    mkdir(SHOP_DATA_DIR, 0777);
    int f = open(TILE_STAMP, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (f < 0) return;
    (void)!write(f, id, strlen(id));
    close(f);
}

/* ------------------------------------------------------- install-path cleanup
 *
 * What an install leaves behind on this console, and what is safe to remove:
 *
 *   /data/pkg-mutant-shop/pms-tile.pkg   OURS. Staged so the console could fetch it. Once the
 *                                        console has its own copy under /user/app it is dead
 *                                        weight - and it is a second copy of a package this very
 *                                        ELF already carries, so it is 6.6 MB of the same thing
 *                                        twice.
 *   /user/bgft/task/<id>                 the console's, one directory per registered task. Swept
 *                                        by bgft_sweep_ours(), which only ever releases a task
 *                                        whose own record names one of our routes.
 *   the /pkgfile token table             in memory; it goes when the process does.
 *
 * NEVER removed: a package the console might still be reading - which is why this only runs after
 * app.pkg proves the install finished, not on a return code; anything under /user/app or
 * /user/appmeta, because that IS the installed app; and anything at all outside
 * /data/pkg-mutant-shop, because nothing this shop writes lives anywhere else. In particular it
 * does not touch the jailbreak's folders, which are not ours to tidy.
 */
static void install_path_cleanup(const char *why) {
    /* EVERY staged copy of the dashboard app in our own directory, not just the one this build
       happens to name. A package staged by an older build, or put there by hand while working out
       why an install stalled, is the same 6.6 MB of dead weight - and the name pattern is ours
       alone. Bounded to SHOP_DATA_DIR and to "pms-tile*.pkg": nothing else is ever a candidate. */
    DIR *d = opendir(SHOP_DATA_DIR);
    if (!d) return;
    struct dirent *e;
    int gone = 0;
    long long freed = 0;
    while ((e = readdir(d))) {
        const char *n = e->d_name;
        size_t ln = strlen(n);
        if (strncmp(n, "pms-tile", 8)) continue;
        if (ln < 4 || strcmp(n + ln - 4, ".pkg")) continue;
        char p[700];
        struct stat st;
        snprintf(p, sizeof(p), "%s/%s", SHOP_DATA_DIR, n);
        if (stat(p, &st) != 0 || !S_ISREG(st.st_mode)) continue;
        if (unlink(p) == 0) { gone++; freed += (long long)st.st_size; }
        else ilog("cleanup: could not remove %s", p);
    }
    closedir(d);
    if (gone)
        ilog("cleanup: removed %d staged package%s (%lld bytes) - %s",
             gone, gone == 1 ? "" : "s", freed, why);
}

/* Decide, then act. Runs on its own thread once the server is listening, because the install lane
   serves the package to the console from this very process - the socket has to be up first. */
static void *tile_thread(void *unused) {
    (void)unused;
    char have[16] = {0};
    long long bytes = 0, base_mtime = 0;
    int had = app_pkg_facts(PS4_TILE_TID, &bytes, &base_mtime);
    tile_installed_ver(have, sizeof(have));
    int hv = tile_ver_num(have), wv = tile_ver_num(PS4_TILE_VER);

    if (bytes > 0 && hv >= wv) {
        /* A LAUNCH TICKET IS PART OF BEING INSTALLED. Without one the icon is on the home screen and
           does nothing - the console refuses to start a title BGFT has no task for. Reinstalling is
           what creates one, and it costs four seconds from a copy this ELF already carries, so an
           icon that cannot open is always worth that. */
        int tickets = ticket_count_for(PS4_TILE_TID);
        if (tile_bytes_match() && tickets > 0) {
            ilog("tile: already installed at %s (carrying %s) - nothing to do",
                 have[0] ? have : "?", PS4_TILE_VER);
            return NULL;
        }
        if (tile_bytes_match() && tickets == 0) {
            ilog("tile: installed at %s and correct, but it has no launch ticket - the console would "
                 "refuse to open it, so installing it again to make one", have[0] ? have : "?");
        } else {
        if (tile_repair_tried()) {
            ilog("tile: the console's copy still differs from this build after one repair "
                 "- leaving it alone");
            return NULL;
        }
        /* NOT MARKED HERE. This used to burn the one attempt before making it, and THIS lane is
           the one that cannot work: the download service refuses a package the console serves to
           itself once PlayStation Network is blocked (0x80991404). So a failure here consumed the
           attempt that the PC - which the service does accept a package from - was going to make.
           The stamp is set when an install actually finishes, in job_refresh. */
        ilog("tile: the console reports %s but its copy is not the package this build carries "
             "- reinstalling it", have[0] ? have : "?");
        }
    } else if (bytes > 0) {
        ilog("tile: installed at %s, this build carries %s - updating", have[0] ? have : "?",
             PS4_TILE_VER);
    } else {
        ilog("tile: not installed - installing %s", PS4_TILE_VER);
    }

    char detail[300] = {0};
    int rc = tile_stage_and_install(detail, sizeof(detail));
    if (rc != 0) {
        ilog("tile: could not install it - %s", detail);
        return NULL;
    }
    /* Wait for the console to finish, then check the FILE rather than the return code. */
    for (int i = 0; i < 90; i++) {
        sleep(2);
        /* CHANGED, not merely present. This loop used to fire on the first tick of an UPDATE,
           because app.pkg was already there from the version being replaced - and it then deleted
           the staged package the console was in the middle of downloading. */
        long long nsz = 0, nmt = 0;
        int now_has = app_pkg_facts(PS4_TILE_TID, &nsz, &nmt);
        if (now_has && (!had || nsz != bytes || nmt != base_mtime)) {
            ilog("tile: installed (%lld bytes on disk)", nsz);
            /* THE STAGED COPY GOES NOW. The console has its own copy under /user/app; ours
               was only ever the thing we served to it, and leaving it behind means carrying
               the package twice on the drive for ever - once there and once inside this very
               ELF. Only after the install is PROVEN by app.pkg on disk, never on a return
               code. */
            install_path_cleanup("the dashboard app finished installing");
            notify("PKG MUTANT SHOP is on your home screen\nOpen it from there any time");
            return NULL;
        }
        job_refresh();
        pthread_mutex_lock(&g_job_lock);
        int failed = g_job.active && !strcmp(g_job.state, "error");
        char why[200];
        snprintf(why, sizeof(why), "%s", g_job.msg);
        pthread_mutex_unlock(&g_job_lock);
        if (failed) { ilog("tile: the console stopped the install - %s", why); return NULL; }
    }
    ilog("tile: the console never finished installing it");
    return NULL;
}

static void tile_start(void) {
    pthread_t t;
    pthread_attr_t at;
    pthread_attr_init(&at);
    pthread_attr_setstacksize(&at, 256 * 1024);
    if (pthread_create(&t, &at, tile_thread, NULL) == 0) pthread_detach(t);
    pthread_attr_destroy(&at);
}
#else
/* The LITE build is the payload that travels inside the home-screen app's package, so it
   carries no package of its own - that would be a package containing itself. Everything
   else about it is identical, which is the point: pressing the icon starts the same shop. */
static void tile_start(void) { ilog("tile: lite build - no package to install"); }
#endif

static void serve_static(int fd, const char *path, const char *req) {
    if (strstr(path, "..")) { send_status(fd, "403 Forbidden", "text/plain", "no"); return; }
    char full[700];
    if (!strcmp(path, "/") || path[0] == 0)
        snprintf(full, sizeof(full), "%s/index.html", WEB_ROOT);
    else
        snprintf(full, sizeof(full), "%s%s", WEB_ROOT, path);
    if (send_file_req(fd, full, req) != 0)
        send_status(fd, "404 Not Found", "text/plain", "not found");
}

static void handle_get(int fd, const char *rawpath, const char *req) {
    char path[1024];
    snprintf(path, sizeof(path), "%s", rawpath);
    char *q = strchr(path, '?');
    if (q) *q = 0;

    if (!strcmp(path, "/api/health")) {
        /* MEASURED, not asserted - see ftp_live_port(). ftp_port is 0 when nothing is listening,
           and the page prints the port only when ftp_online is true, so that 0 is never shown. */
        int fport = ftp_live_port();
        /* running_title IS ANSWERED NOW. It stayed empty for as long as this file could not
           find a PS4 signature it was allowed to trust - the right call, and the note that used to
           sit here explained why. The signatures turned out to be readable after all, in the
           payload SDK's own sample and in OpenOrbis, and the one thing they disagreed about was
           measured on this console rather than picked. See running_title_id(). Empty still means
           "no game running", which is what the page has always treated it as. */
        char rt_now[16];
        running_title_id(rt_now, sizeof(rt_now));
        char csig[96];      /* four %lld (20 each) and three dashes is 83 - sized from the format */
        content_sig(csig, sizeof(csig));
        char out[900];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"on_console\":true,\"thumb_route\":true,"
                 "\"server\":\"on-console\",\"platform\":\"ps4\","
                 "\"connected\":true,\"version\":\"%s\",\"built\":\"%s %s\",\"ps5_ip\":\"%s\","
                 "\"lan_ip\":\"%s\",\"companion_port\":%d,\"shop_port\":%d,"
                 "\"engine\":\"pms-bgft\",\"engine_ready\":%s,\"ftp_online\":%s,\"ftp_port\":%d,"
                 "\"shadowmount\":false,\"shadowmount_port\":0,\"running_title\":\"%s\","
                 "\"console_id\":\"%s\","
                 "\"apps_sig\":\"%s\","
                 "\"uptime_s\":%lld,\"conns\":%lld}",
                 SHOP_VERSION, __DATE__, __TIME__, lan_ip_str(), lan_ip_str(), PORT, PORT,
                 g_bgft_ready ? "true" : "false",
                 fport ? "true" : "false", fport, rt_now, console_id(), csig,
                 g_boot_ms ? (now_ms() - g_boot_ms) / 1000 : 0, g_conns_served);
        send_json(fd, out);
        return;
    }
    /* WHICH INSTALL FUNCTIONS THIS FIRMWARE ACTUALLY HAS.
     *
     * A diagnostic, and it exists because of a measured problem rather than curiosity. Every install
     * this shop starts goes through BGFT, and BGFT asks PlayStation Network whether the title has a
     * newer version BEFORE it downloads: when it finds one it builds a two-part task, fetches our
     * package, cannot fetch the update, and abandons the whole thing. Measured on this console -
     * Bluey's Quest for the Gold Pen, a 737,869,824-byte package, BGFT asked for 1,348,665,344 and
     * gave up at 89%; Castle Crashers Remastered the same at 2%. Riptide GP2 installed first time
     * because the Store has no newer version of it. That is the difference, and it is not ours.
     *
     * klog shows the console finishing an install with two calls of its own, AFTER the download:
     *     begin AppPrePromotePkgExt(/user/bgft/task/<id>/app.pkg)
     *     begin sceAppInstaller::AppInstallApp(/user/bgft/task/<id>, 0)
     * If those are reachable from here, a package already on this console could be installed
     * directly - no BGFT, no download, and no patch check to fail. This route answers only whether
     * the symbols exist. It resolves names and reports addresses; it calls nothing. Guessing a
     * signature and calling it is how this project once crashed a console, so the calling comes
     * later, from the answer, not from a hunch. */
    if (!strcmp(path, "/api/engine/symprobe")) {
        char names[2048] = {0};
        if (!qparam(rawpath, "names", names, sizeof(names)) || !names[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"pass ?names=a,b,c\"}");
            return;
        }
        char out[4096];
        int len = snprintf(out, sizeof(out), "{\"ok\":true,\"libs\":%d,\"syms\":{", g_dl_n);
        char *p = names;
        int first = 1;
        while (p && *p && len < (int)sizeof(out) - 220) {
            char *comma = strchr(p, ',');
            if (comma) *comma = 0;
            while (*p == ' ') p++;
            if (*p) {
                void *a = dlsym_any(p);
                char esc[200];
                json_escape(p, esc, sizeof(esc));
                len += snprintf(out + len, sizeof(out) - len, "%s\"%s\":%s",
                                first ? "" : ",", esc, a ? "true" : "false");
                first = 0;
            }
            if (!comma) break;
            p = comma + 1;
        }
        snprintf(out + len, sizeof(out) - len, "}}");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/state")) {
        /* The same measured answer /api/health gives. All the routes that name this port move
           together: correcting one and leaving the others asserting is the exact miss that cost
           this project a follow-up release when the ShadowMount port was fixed everywhere but two. */
        int fport = ftp_live_port();
        /* ASK THE CONSOLE BEFORE ANSWERING. job_refresh() is what turns a transfer that has ended
           into state "installed" or "error"; without it this route reports whatever the job last
           looked like, which is how a finished install kept reporting "busy" even after the busy
           test was corrected. /api/engine/job has always refreshed first - these two describe the
           same job and must not disagree. */
        job_refresh();
        pthread_mutex_lock(&g_job_lock);
        int running = job_running();
        pthread_mutex_unlock(&g_job_lock);
        char out[700];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"platform\":\"ps4\",\"mode\":\"bgft\",\"ours\":true,"
                 "\"name\":\"PKG MUTANT SHOP engine\",\"shop_port\":%d,\"shop_ok\":true,"
                 "\"shop_version\":\"%s\",\"ready\":%s,\"busy\":%s,\"busy_for\":0,"
                 "\"console_ip\":\"%s\",\"shadowmount\":false,\"ftp_port\":%d,"
                 "\"state\":\"%s\",\"detail\":\"%s\",\"how\":\"%s\"}",
                 PORT, SHOP_VERSION, g_bgft_ready ? "true" : "false",
                 /* A FINISHED JOB IS NOT A RUNNING ONE. `active` stays set after a job ends so
                    the panel can still show how it went, and the gate that accepts a new install
                    already knows that - it tests active AND a non-terminal state (see the check
                    beside g_job.held). This line did not, so once the payload installed its own
                    home-screen icon at boot the engine reported "busy" for ever, which reads as a
                    wedged install lane on a console that is perfectly free. */
                 running ? "true" : "false", lan_ip_str(), fport,
                 /* "engine-down", NOT "shop-down". The shop is plainly up - it is serving the
                    page this sentence is read on. What did not start is the console's transfer
                    service, and saying "shop-down" sent the reader off to re-run the jailbreak
                    instead of closing and reopening the app, which is the thing that would help.
                    The page has had an engine-down branch since the engine panel learned about
                    the PS4; this route was still answering the old word. */
                 g_bgft_ready ? (running ? "busy" : "ready") : "engine-down",
                 g_bgft_ready ? (running ? "An install is running on the console right now."
                                              : "Ready - installs can start straight away.")
                              : "The background transfer service did not start on this PS4.",
                 "The PS4 downloads and installs the package itself, straight from this PC.");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/library")) {
        char *j = build_library_json();
        if (j) { send_json(fd, j); free(j); }
        else send_json(fd, "{\"ok\":false,\"error\":\"This PS4 is low on memory - restart the "
                            "console, run the jailbreak and load the shop again\"}");
        return;
    }
    if (!strcmp(path, "/api/installed")) {
        char *j = build_installed_json();
        if (j) { send_json(fd, j); free(j); }
        else send_json(fd, "{\"ok\":false,\"error\":\"This PS4 is low on memory - restart the "
                            "console, run the jailbreak and load the shop again\"}");
        return;
    }
    if (!strcmp(path, "/api/devices"))  { send_devices(fd); return; }
    if (!strcmp(path, "/api/storage"))  { send_storage(fd); return; }

    if (!strncmp(path, "/icon/", 6) || !strncmp(path, "/api/icon/", 10)) {
        const char *t = path + (path[1] == 'i' ? 6 : 10);
        char tid[16];
        snprintf(tid, sizeof(tid), "%.9s", t);
        char ip[600];
        if (icon_path_for(tid, ip, sizeof(ip)) && send_file(fd, ip) == 0) return;
        send_status(fd, "404 Not Found", "text/plain", "no icon");
        return;
    }
    /* GET /thumb/<TID>.jpg - the card-sized cover.
     *
     * THIS CONSOLE MAKES IT ITSELF NOW. It could not before: there is no JPEG, PNG or zlib
     * library in this SDK and the console's other copy of the art is a BC7-compressed DDS, so the
     * cache was filled only by a companion - and with every PC off it was empty, so every card
     * fell back to the full-size icon0.png. Measured here: 23 covers, 7.1 MB, mean 325 KB, into
     * the browser that least tolerates it. shared/pms_thumb.h writes a 320 px JPEG instead.
     *
     * MADE INLINE when the boot sweep has not reached this title yet, and only while that sweep
     * is not running, so a request and the sweep cannot write the same file.
     *
     * AND THE FALLBACK STAYS, because it is what makes asking always safe. A 404 runs the cover's
     * error path and leaves initials in its place until the grid is rebuilt; a shape pms_thumb.h
     * will not decode still gets the original rather than nothing. */
    if (!strncmp(path, "/thumb/", 7)) {
        char tid[16];
        snprintf(tid, sizeof(tid), "%.9s", path + 7);
        char tp[600];
        struct stat tst;
        snprintf(tp, sizeof(tp), "%s/thumbs/%s.jpg", SHOP_DATA_DIR, tid);
        /* `req`, not NULL: this is the biggest thing this server sends and the art for a title id
           never changes, so a reload should cost a header rather than the body again. */
        if (stat(tp, &tst) == 0 && S_ISREG(tst.st_mode) && tst.st_size > 0
            && send_file_req(fd, tp, req) == 0) return;
        if (!g_thumb_busy && thumb_fill_one(tid) == 0
            && send_file_req(fd, tp, req) == 0) return;
        char ip2[600];
        if (icon_path_for(tid, ip2, sizeof(ip2)) && send_file_req(fd, ip2, req) == 0) return;
        send_status(fd, "404 Not Found", "text/plain", "no icon");
        return;
    }

    /* ---- the install lane, named exactly as the PS5 build names it ---- */
    if (!strcmp(path, "/api/engine/install-spawn") || !strcmp(path, "/api/engine/install-url")) {
        char uri[1024] = {0}, name[240] = {0};
        if (!qparam(rawpath, "uri", uri, sizeof(uri)) || !uri[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"no package was named\"}");
            return;
        }
        qparam(rawpath, "name", name, sizeof(name));
        char cid[80] = {0}, szs[32] = {0}, ptype[24] = {0};
        qparam(rawpath, "cid", cid, sizeof(cid));
        qparam(rawpath, "size", szs, sizeof(szs));
        qparam(rawpath, "type", ptype, sizeof(ptype));
        long long psize = szs[0] ? atoll(szs) : 0;
        /* Ask the console how the last job actually ended before deciding we are busy. The state
           in g_job is only as fresh as the last refresh, and nothing refreshes it unless someone
           polls /api/engine/job - so a finished or failed install looked "still running" to the
           very next request and the shop refused it. */
        job_refresh();
        long long sclaim = job_claim(JOB_CLAIM_FRESH, NULL);
        if (!sclaim) {
            send_status(fd, "409 Conflict", "application/json",
                        "{\"ok\":false,\"busy\":true,\"error\":\"An install is already running on this PS4\"}");
            return;
        }
        char err[256] = {0};
        OrbisBgftTaskId task = BGFT_INVALID_TASK_ID;
        int rc = bgft_install_url(uri, name, cid, psize, ptype, err, sizeof(err), &task);
        if (rc != 0) {
            job_claim_abort(sclaim);
            char esc[300], out[600];
            json_escape(err, esc, sizeof(esc));
            snprintf(out, sizeof(out), "{\"ok\":false,\"queued\":false,\"error\":\"%s\"}", esc);
            send_json(fd, out);
            return;
        }
        pthread_mutex_lock(&g_job_lock);
        if (!job_claim_is_mine_locked(sclaim)) {   /* taken from us - see install_local_pkg */
            pthread_mutex_unlock(&g_job_lock);
            bgft_release(task);
            send_json(fd, "{\"ok\":false,\"queued\":false,"
                          "\"error\":\"That install was stopped before it started\"}");
            return;
        }
        memset(&g_job, 0, sizeof(g_job));
        g_job.active = 1;
        g_job.task = task;
        g_job.job_id = now_ms();
        g_job.started_ms = now_ms();
        g_job.expect = psize;
        snprintf(g_job.uri, sizeof(g_job.uri), "%s", uri);
        snprintf(g_job.name, sizeof(g_job.name), "%s", name);
        /* WHAT A RETRY WILL NEED. JOB_CLAIM_RETRY hands these to bgft_install_url, and only the
           queued lane used to write them - so retrying an ordinary install re-registered with no
           content id and size 0, which is not the package that failed. */
        snprintf(g_job.want_cid, sizeof(g_job.want_cid), "%s", cid);
        snprintf(g_job.want_type, sizeof(g_job.want_type), "%s", ptype);
        g_job.want_size = psize;
        /* The title id comes out of the CONTENT id first, by STRUCTURE - everything between the
           first '-' and the '_' - because that works for any title id there is. The older reader
           below only recognises a game's (CUSA/NPXS), and this shop installs one package that is
           not a game: its own dashboard app, PKGM00001. That install ran to completion on the
           console and the job never said so, because the title id it was looking for was "". Fall
           back to the game-shaped search for a request that carries no content id at all. */
        tid_from_cid(cid, g_job.tid, sizeof(g_job.tid));
        if (!g_job.tid[0])
            if (!pkg_content_id_from_url_name(name, g_job.tid, sizeof(g_job.tid)))
                pkg_content_id_from_url_name(uri, g_job.tid, sizeof(g_job.tid));
        /* The companion names the package type; turn it back into the category the console uses,
           so the finished check looks in the right place for an update or an add-on. */
        if (!strcmp(ptype, "PS4GP"))      snprintf(g_job.cat, sizeof(g_job.cat), "gp");
        else if (!strcmp(ptype, "PS4AC")) snprintf(g_job.cat, sizeof(g_job.cat), "ac");
        else if (!strcmp(ptype, "PS4GD")) snprintf(g_job.cat, sizeof(g_job.cat), "gd");
        job_baseline_locked();
        snprintf(g_job.state, sizeof(g_job.state), "downloading");
        snprintf(g_job.msg, sizeof(g_job.msg), "The PS4 is downloading and installing it");
        g_job_claim = 0;
        long long jid = g_job.job_id;
        pthread_mutex_unlock(&g_job_lock);

        notifyf("%s is installing\nThe PS4 is downloading it now - watch your home screen",
                name[0] ? name : "Your game");
        char out[420];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"queued\":true,\"job_id\":%lld,\"task\":%d,\"rc\":\"0x00000000\"}",
                 jid, (int)task);
        send_json(fd, out);
        return;
    }
    /* INSTALLING SOMETHING THE CONSOLE ALREADY HAS - a stick, or a package copied to the drive.
       It goes through exactly the same installer as a package from the PC: the file is registered,
       served back to the console over loopback, and that URL is handed to BGFT. No separate lane, no
       second verdict path, and nothing that writes a title without its data behind it. */
    /* ROUTES THE PS5 ANSWERS AND THIS CONSOLE CANNOT, answered in words.
     *
     * Unknown /api/ paths fall through to a bare {} so an older page keeps working, and that is
     * exactly what makes a missing route dangerous: the caller reads {} as "nothing to report"
     * rather than "this console does not do that". Each of these is a real button somewhere, so
     * each says what is true instead of going quiet. The POST side of cheats and mods already did
     * this; the GET side did not, which is how /api/cheat/paths answered {} to the settings panel.
     */
    /* /api/mem is here rather than in a second block further down. There WAS a second copy of
       this refusal, and because this one runs first it only ever saw /api/mem - where it answered
       the same refusal WITHOUT the "platform" key, so the one path that reached it got a different
       shape from every other path in the set. One list, one answer. */
    /* ---- THE TWO MEASUREMENTS THE CHEAT ENGINE IS WAITING ON -------------------------------
     * Both are read-only and neither touches a game. They are under /api/engine/ deliberately:
     * /api/mem is the refused prefix below, and a diagnostic must not answer on a path the app
     * uses for the real thing. They are also the ONLY two routes in this file that issue a syscall
     * by number, which is why the comment above pms_mdbg_op() is where the reasoning lives.
     */
    if (!strcmp(path, "/api/engine/ptraceprobe")) {
        /* Is ptrace implemented for us? Nothing is attached to - see the file note above. The pid
           is picked out of the live process list precisely so that "it does not exist" is measured
           rather than assumed, and the raw errno is reported rather than a verdict. */
        int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PROC, 0 };
        size_t need = 0;
        int live[512];
        int nlive = 0;
        if (!sysctl(mib, 4, NULL, &need, NULL, 0) && need) {
            need += need / 8 + 8192;
            char *buf = (char *)malloc(need);
            if (buf) {
                if (!sysctl(mib, 4, buf, &need, NULL, 0)) {
                    for (char *p = buf; p < buf + need && nlive < 512; ) {
                        struct kinfo_proc *ki = (struct kinfo_proc *)p;
                        if (ki->ki_structsize <= 0 || p + ki->ki_structsize > buf + need) break;
                        p += ki->ki_structsize;
                        live[nlive++] = (int)ki->ki_pid;
                    }
                }
                free(buf);
            }
        }
        /* The first number above every live pid, so it cannot collide with one that exists. */
        int free_pid = 30000;
        for (int i = 0; i < nlive; i++) if (live[i] >= free_pid) free_pid = live[i] + 1;

        errno = 0;
        int rc = ptrace(PT_ATTACH, (pid_t)free_pid, 0, 0);
        int er = errno;

        /* Named, because an errno number in a JSON field is a thing nobody can read at a glance -
           and the whole value of this probe is in WHICH refusal it is. */
        const char *meaning =
            (er == ENOSYS) ? "not implemented for us - ptrace is a dead end like mdbg"
          : (er == ESRCH)  ? "implemented and reachable - it looked for the process and did not "
                             "find it, which is the answer we wanted; credentials against a REAL "
                             "target are still unproven"
          : (er == EPERM)  ? "refused before it even looked - a harder no than ESRCH is a yes"
          : (er == EINVAL) ? "reached the call and rejected the request shape"
          : "unexpected - read the number";

        char out[520];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"platform\":\"ps4\",\"call\":\"ptrace\",\"request\":\"PT_ATTACH\","
                 "\"pid_tried\":%d,\"pid_exists\":false,\"live_procs\":%d,\"rc\":%d,\"errno\":%d,"
                 "\"meaning\":\"%s\",\"attached_to_anything\":false}",
                 free_pid, nlive, rc, er, meaning);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/memprobe")) {
        /* OUR OWN PROCESS, OUR OWN BYTES, READ ONLY. The source is a local array we just filled;
           the destination is a second local array. If the kernel refuses the call, nothing has
           been touched; if it accepts it, `match` is 1 and the whole engine is unblocked. A game
           is never a valid target for this route - see /api/engine/proclist for finding one, and
           note that reading a game would prove nothing this does not. */
        volatile unsigned char src[16];
        unsigned char dst[16];
        for (int i = 0; i < 16; i++) src[i] = (unsigned char)(0xA5 ^ (i * 17));
        memset(dst, 0, sizeof(dst));

        pms_mdbg_args_t a;
        pms_mdbg_res_t  r;
        a.pid = (int)getpid();
        a.src = (unsigned long)(void *)src;
        a.dst = (unsigned long)dst;
        a.len = sizeof(dst);
        long rc = pms_mdbg_op(PMS_MDBG_READ, &a, &r);

        int match = 1;
        for (int i = 0; i < 16; i++)
            if (dst[i] != (unsigned char)(0xA5 ^ (i * 17))) { match = 0; break; }

        char hex[48];
        for (int i = 0; i < 8; i++) snprintf(hex + i * 2, 3, "%02x", dst[i]);
        hex[16] = 0;

        char out[420];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"platform\":\"ps4\",\"call\":\"mdbg_call\",\"syscall\":%d,"
                 "\"pid\":%d,\"rc\":%ld,\"status\":%d,\"len\":%lu,\"match\":%s,\"first8\":\"%s\","
                 "\"usable\":%s}",
                 PMS_SYS_MDBG_CALL, a.pid, rc, r.status, (unsigned long)r.len,
                 match ? "true" : "false", hex,
                 (rc >= 0 && match) ? "true" : "false");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/credprobe")) {
        /* REVERSIBLE. Escalate with GoldHEN's own pair, test mdbg, restore, report. Guesses no
           credential value; modifies no game memory. See scratchpad/credprobe.py for the why. */
        unsigned char src[16], dst[16];
        for (int i = 0; i < 16; i++) src[i] = (unsigned char)(0x5A ^ (i * 11));

        /* mdbg self-read BEFORE (baseline - expected to fail as it always has). */
        pms_mdbg_args_t a; pms_mdbg_res_t r;
        memset(dst, 0, sizeof(dst));
        a.pid = (int)getpid(); a.src = (unsigned long)src; a.dst = (unsigned long)dst; a.len = 16;
        long before = pms_mdbg_op(PMS_MDBG_READ, &a, &r);

        /* Escalate. jb is filled by the kernel; we treat it as opaque. */
        gh_jailbreak_backup_t jb;
        memset(&jb, 0, sizeof(jb));
        long jbrc = gh_orbis_syscall(GH_SDK_SYSCALL, GH_SDK_CMD_JAILBREAK, &jb);

        /* mdbg self-read WHILE escalated. */
        memset(dst, 0, sizeof(dst));
        long during = pms_mdbg_op(PMS_MDBG_READ, &a, &r);
        int during_match = 1;
        for (int i = 0; i < 16; i++) if (dst[i] != src[i]) { during_match = 0; break; }

        /* mdbg CROSS-PROCESS read (read-only) while escalated: read a few bytes of another live
           process. This is the real question - can we reach a process that is not us. No write. */
        int other_pid = 0; char other_comm[24] = {0};
        long xrc = 1; int xok = 0;
        {
            int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PROC, 0 };
            size_t need = 0;
            if (!sysctl(mib, 4, NULL, &need, NULL, 0) && need) {
                need += need / 8 + 8192;
                char *b = (char *)malloc(need);
                if (b && !sysctl(mib, 4, b, &need, NULL, 0)) {
                    for (char *p = b; p < b + need; ) {
                        struct kinfo_proc *ki = (struct kinfo_proc *)p;
                        if (ki->ki_structsize <= 0 || p + ki->ki_structsize > b + need) break;
                        p += ki->ki_structsize;
                        if ((int)ki->ki_pid != (int)getpid() && ki->ki_pid > 1) {
                            other_pid = (int)ki->ki_pid;
                            snprintf(other_comm, sizeof(other_comm), "%.19s", ki->ki_comm);
                            break;
                        }
                    }
                }
                free(b);
            }
            if (other_pid > 0) {
                unsigned char xbuf[16];
                memset(xbuf, 0, sizeof(xbuf));
                pms_mdbg_args_t xa; pms_mdbg_res_t xr;
                /* read the other process's low code region read-only; 0x400000 is the PS4 no-ASLR
                   image base and is a valid readable address in a normal process. */
                xa.pid = other_pid; xa.src = 0x400000UL; xa.dst = (unsigned long)xbuf; xa.len = 16;
                xrc = pms_mdbg_op(PMS_MDBG_READ, &xa, &xr);
                xok = (xrc >= 0 && xr.status == 0);
            }
        }

        /* Restore, immediately, from the untouched backup. */
        long unrc = gh_orbis_syscall(GH_SDK_SYSCALL, GH_SDK_CMD_UNJAILBREAK, &jb);

        /* mdbg self-read AFTER restore - so we can SEE we came back to the baseline. */
        memset(dst, 0, sizeof(dst));
        long after = pms_mdbg_op(PMS_MDBG_READ, &a, &r);

        char out[900];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"platform\":\"ps4\",\"reversible\":true,"
                 "\"jailbreak_rc\":%ld,\"unjailbreak_rc\":%ld,"
                 "\"authid\":\"0x%016lx\",\"caps0\":\"0x%016lx\",\"caps1\":\"0x%016lx\","
                 "\"uid\":%u,"
                 "\"mdbg_self_before\":%ld,\"mdbg_self_during\":%ld,\"during_match\":%s,"
                 "\"mdbg_self_after\":%ld,"
                 "\"xproc_pid\":%d,\"xproc_comm\":\"%s\",\"mdbg_xproc_rc\":%ld,"
                 "\"mdbg_xproc_ok\":%s,"
                 "\"unlocked\":%s}",
                 jbrc, unrc,
                 jb.cr_paid, jb.cr_caps[0], jb.cr_caps[1], jb.cr_uid,
                 before, during, during_match ? "true" : "false", after,
                 other_pid, other_comm, xrc, xok ? "true" : "false",
                 (xok || during_match) ? "true" : "false");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/ghrwprobe")) {
        /* THE DECISIVE READ-ONLY TEST. Nothing here is a destination for a game's memory. It asks
           the gateway three questions and reports the raw answers; the console settles what they
           mean, exactly as /api/engine/proclist settles the app-info layout. */

        /* 1. The SDK version. A quirk worth noting: this errors on some builds while the info
              command below works fine, so "is the gateway usable" is decided by the info call, not
              by this. Reported for the record. */
        long ver = gh_orbis_syscall(GH_SDK_SYSCALL, GH_SDK_CMD_VERSION, (void *)0);

        /* 2. Our OWN process info. pid=0 in, so what comes back tells us whether the kernel FILLS
              the caller's identity (pid is an OUTPUT - bad, means caller-only) or would have used
              a pid we set (pid is an INPUT selector - good). */
        gh_proc_info_t self;
        memset(&self, 0, sizeof(self));
        long self_rc = gh_orbis_syscall(GH_SDK_SYSCALL, GH_SDK_CMD_PROCESS_INFO, &self);

        /* 2b. THE DISAMBIGUATOR, and it needs no game running. Ask about a DIFFERENT process by
               pid - the first live pid that is not ours. If the answer describes THAT process, the
               pid field is an input selector and the gateway is cross-process; if it comes back
               describing us again, the pid is caller-only and no outside-in route exists here. */
        int other_pid = 0;
        char other_comm[24] = {0};
        {
            int mib2[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PROC, 0 };
            size_t need2 = 0;
            if (!sysctl(mib2, 4, NULL, &need2, NULL, 0) && need2) {
                need2 += need2 / 8 + 8192;
                char *b2 = (char *)malloc(need2);
                if (b2 && !sysctl(mib2, 4, b2, &need2, NULL, 0)) {
                    for (char *p = b2; p < b2 + need2; ) {
                        struct kinfo_proc *ki = (struct kinfo_proc *)p;
                        if (ki->ki_structsize <= 0 || p + ki->ki_structsize > b2 + need2) break;
                        p += ki->ki_structsize;
                        if ((int)ki->ki_pid != (int)getpid() && ki->ki_pid > 1) {
                            other_pid = (int)ki->ki_pid;
                            snprintf(other_comm, sizeof(other_comm), "%.19s", ki->ki_comm);
                            break;
                        }
                    }
                }
                free(b2);
            }
        }
        gh_proc_info_t other;
        long other_rc = -1;
        int other_is_selected = 0;
        memset(&other, 0, sizeof(other));
        if (other_pid > 0) {
            other.pid = other_pid;
            other_rc = gh_orbis_syscall(GH_SDK_SYSCALL, GH_SDK_CMD_PROCESS_INFO, &other);
            /* the returned pid matches what we asked = pid is an input selector */
            other_is_selected = (other_rc == 0 && other.pid == other_pid && other.pid != self.pid);
        }

        /* 3. The RUNNING GAME's process info, asked by pid. If the gateway is cross-process this
              comes back describing the GAME (its titleid, its base_address); if the gateway only
              ever sees the caller, it comes back describing ScePartyDaemon no matter what pid we
              put in. Either answer is decisive and neither writes anything. */
        int game_pid = 0;
        char game_comm[24] = {0};
        {
            int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PROC, 0 };
            size_t need = 0;
            int (*getappinfo)(pid_t, void *) =
                (int (*)(pid_t, void *))dlsym_any("sceKernelGetAppInfo");
            int (*bigapp)(void) = (int (*)(void))dlsym_any("sceSystemServiceGetAppIdOfBigApp");
            int fg = bigapp ? bigapp() : -1;
            if (fg > 0 && getappinfo && !sysctl(mib, 4, NULL, &need, NULL, 0) && need) {
                need += need / 8 + 8192;
                char *buf = (char *)malloc(need);
                if (buf && !sysctl(mib, 4, buf, &need, NULL, 0)) {
                    for (char *p = buf; p < buf + need; ) {
                        struct kinfo_proc *ki = (struct kinfo_proc *)p;
                        if (ki->ki_structsize <= 0 || p + ki->ki_structsize > buf + need) break;
                        p += ki->ki_structsize;
                        unsigned char ai[0x100];
                        memset(ai, 0, sizeof(ai));
                        if (getappinfo(ki->ki_pid, ai)) continue;
                        if (*(unsigned int *)ai != (unsigned int)fg) continue;
                        game_pid = (int)ki->ki_pid;
                        snprintf(game_comm, sizeof(game_comm), "%.19s", ki->ki_comm);
                        break;
                    }
                }
                free(buf);
            }
        }

        gh_proc_info_t game;
        long game_rc = -1;
        int game_base_nonzero = 0, game_base_differs = 0;
        memset(&game, 0, sizeof(game));
        if (game_pid > 0) {
            game.pid = game_pid;
            game_rc = gh_orbis_syscall(GH_SDK_SYSCALL, GH_SDK_CMD_PROCESS_INFO, &game);
            game_base_nonzero = (game.base_address != 0);
            game_base_differs = (game.base_address != self.base_address);
        }

        /* Sanitise the text fields before they go into JSON: the structs are packed binary and a
           stray control byte would break the document. Non-printables become '.'. */
        char sn[48], stid[24], gn[48], gtid[24];
        snprintf(sn, sizeof(sn), "%.39s", self.name);
        snprintf(stid, sizeof(stid), "%.15s", self.titleid);
        snprintf(gn, sizeof(gn), "%.39s", game.name);
        snprintf(gtid, sizeof(gtid), "%.15s", game.titleid);
        for (char *c = sn;   *c; c++) if (*c < 32 || *c > 126) *c = '.';
        for (char *c = stid; *c; c++) if (*c < 32 || *c > 126) *c = '.';
        for (char *c = gn;   *c; c++) if (*c < 32 || *c > 126) *c = '.';
        for (char *c = gtid; *c; c++) if (*c < 32 || *c > 126) *c = '.';

        char on[48], otid[24];
        snprintf(on, sizeof(on), "%.39s", other.name);
        snprintf(otid, sizeof(otid), "%.15s", other.titleid);
        for (char *c = on;   *c; c++) if (*c < 32 || *c > 126) *c = '.';
        for (char *c = otid; *c; c++) if (*c < 32 || *c > 126) *c = '.';

        char out[1700];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"platform\":\"ps4\",\"syscall\":%d,"
                 "\"version_rc\":%ld,\"version\":\"0x%08lx\","
                 "\"gateway_reachable\":%s,"
                 "\"self\":{\"rc\":%ld,\"pid\":%d,\"name\":\"%s\",\"titleid\":\"%s\","
                 "\"base\":\"0x%lx\"},"
                 "\"other\":{\"asked_pid\":%d,\"comm\":\"%s\",\"rc\":%ld,\"pid\":%d,"
                 "\"name\":\"%s\",\"titleid\":\"%s\",\"base\":\"0x%lx\","
                 "\"is_selected\":%s},"
                 "\"pid_is_input_selector\":%s,"
                 "\"game\":{\"asked_pid\":%d,\"comm\":\"%s\",\"rc\":%ld,\"pid\":%d,"
                 "\"name\":\"%s\",\"titleid\":\"%s\",\"base\":\"0x%lx\","
                 "\"base_nonzero\":%s,\"base_differs_from_self\":%s},"
                 "\"cross_process\":%s}",
                 GH_SDK_SYSCALL,
                 ver, (unsigned long)(ver & 0xffffffff),
                 (self_rc == 0) ? "true" : "false",
                 self_rc, self.pid, sn, stid, self.base_address,
                 other_pid, other_comm, other_rc, other.pid, on, otid, other.base_address,
                 other_is_selected ? "true" : "false",
                 other_is_selected ? "true" : "false",
                 game_pid, game_comm, game_rc, game.pid, gn, gtid, game.base_address,
                 game_base_nonzero ? "true" : "false",
                 game_base_differs ? "true" : "false",
                 (game_pid > 0 && game_rc == 0 && game_base_nonzero && game_base_differs)
                     ? "true" : "false");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/proclist")) {
        /* THE SIGNATURE THIS FILE SAID IT WAS WAITING FOR. /api/health's comment records that
           running_title stays empty because no PS4 signature for "which game is running" could be
           read anywhere - it turns out the SDK ships a working sample that uses two of them, so
           nothing here is inferred: sysctl{CTL_KERN,KERN_PROC,KERN_PROC_PROC,0} walked by each
           record's OWN ki_structsize, and sceKernelGetAppInfo per pid.

           THE LAYOUT IS REPORTED, NOT DECIDED. The two PS4 toolchains on this machine disagree
           about where the title id sits inside the app-info block - offset 12 with 14 bytes in the
           payload SDK's sample, offset 16 with 10 bytes in OpenOrbis - and they cannot both be
           right for 13.52. So both candidates come back as raw text and the console settles it. A
           wrong offset here yields a plausible wrong pid, and a wrong pid is what a memory write
           must never be handed. */
        int mib[4] = { CTL_KERN, KERN_PROC, KERN_PROC_PROC, 0 };
        size_t need = 0;
        if (sysctl(mib, 4, NULL, &need, NULL, 0) || !need) {
            send_json(fd, "{\"ok\":false,\"platform\":\"ps4\",\"error\":\"the kernel would not "
                          "say how many processes there are\"}");
            return;
        }
        need += need / 8 + 8192;                 /* the list can grow between the two calls */
        char *buf = (char *)malloc(need);
        if (!buf) { send_json(fd, "{\"ok\":false,\"error\":\"out of memory\"}"); return; }
        if (sysctl(mib, 4, buf, &need, NULL, 0)) {
            free(buf);
            send_json(fd, "{\"ok\":false,\"platform\":\"ps4\",\"error\":\"the kernel would not "
                          "list the processes\"}");
            return;
        }

        /* sceKernelGetAppInfo is a libkernel export; 0x100 is far larger than either candidate
           layout, so the call cannot write past it whichever one is right. */
        int (*getappinfo)(pid_t, void *) = (int (*)(pid_t, void *))dlsym_any("sceKernelGetAppInfo");
        int (*bigapp)(void) = (int (*)(void))dlsym_any("sceSystemServiceGetAppIdOfBigApp");
        int fg = bigapp ? bigapp() : -1;

        char *out = (char *)malloc(64000);
        if (!out) { free(buf); send_json(fd, "{\"ok\":false,\"error\":\"out of memory\"}"); return; }
        int n = snprintf(out, 64000,
                         "{\"ok\":true,\"platform\":\"ps4\",\"foreground_app_id\":%d,"
                         "\"have_getappinfo\":%s,\"procs\":[", fg,
                         getappinfo ? "true" : "false");
        int first = 1, count = 0;
        for (char *p = buf; p < buf + need && count < 400; ) {
            struct kinfo_proc *ki = (struct kinfo_proc *)p;
            if (ki->ki_structsize <= 0 || p + ki->ki_structsize > buf + need) break;
            p += ki->ki_structsize;
            count++;

            unsigned char info[0x100];
            memset(info, 0, sizeof(info));
            int got = (getappinfo && !getappinfo(ki->ki_pid, info)) ? 1 : 0;
            unsigned int app_id = got ? *(unsigned int *)info : 0u;

            /* Both candidates, printed as text and nothing else. */
            char a12[16], a16[12];
            memset(a12, 0, sizeof(a12)); memset(a16, 0, sizeof(a16));
            for (int i = 0; i < 14; i++) { unsigned char c = info[12 + i]; a12[i] = (c >= 32 && c < 127) ? (char)c : 0; }
            for (int i = 0; i < 10; i++) { unsigned char c = info[16 + i]; a16[i] = (c >= 32 && c < 127) ? (char)c : 0; }

            if (n > 62000) break;
            n += snprintf(out + n, 64000 - n,
                          "%s{\"pid\":%d,\"comm\":\"%.19s\",\"app_id\":%u,\"info\":%s,"
                          "\"tid_at12\":\"%.14s\",\"tid_at16\":\"%.10s\"}",
                          first ? "" : ",", (int)ki->ki_pid, ki->ki_comm, app_id,
                          got ? "true" : "false", a12, a16);
            first = 0;
        }
        snprintf(out + n, 64000 - n, "],\"count\":%d}", count);
        send_json(fd, out);
        free(out);
        free(buf);
        return;
    }
    /* ---- CHEATS AND PATCHES, answered for real ------------------------------------------
     * The engine is ps5-app/onconsole/server.c's, copied into cheat_core.h and kept in sync by
     * tools/ps4_sync_cheat_core.py. Memory is reached through the in-game agent - see the client
     * above it. When no game is running (or the agent is not in the one that is), everything here
     * still LISTS; only toggling needs the agent, which is exactly how the PS5 panel behaves with
     * a game closed.
     */
    if (!strcmp(path, "/api/cheat/list")) {
        /* /api/cheat/list?file=<path>[&pid=&base=]  -> the mods inside one cheat file.
           THE ROUTE THE MODS PANEL IS BUILT FROM. The companion composes its answer from find +
           list + patches; without this one the panel reported "no cheats" for every PS4 title
           while the console itself listed them perfectly. */
        char file[600] = {0};
        if (!qparam(rawpath, "file", file, sizeof(file)) || !file[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"need file\"}"); return;
        }
        char lp[16] = {0}, lb[32] = {0};
        qparam(rawpath, "pid", lp, sizeof(lp));
        qparam(rawpath, "base", lb, sizeof(lb));
        int lpid = lp[0] ? atoi(lp) : 0;
        /* WHAT THE CALLER SAID, THEN WHAT THE AGENT MEASURED, THEN the PS4's no-ASLR load address -
           which is what every offset in our library is relative to when a game does load there. It
           used to jump straight to that address, which quietly turned "I do not know this game's
           base" into "this game is at 0x400000". See agent_image_base(). */
        intptr_t lbase = lb[0] ? (intptr_t)strtoull(lb, NULL, 0) : 0;
        if (!lbase && lpid) lbase = agent_image_base();
        if (!lbase) lbase = (intptr_t)PATCH_NO_ASLR;
        int non_json = 0;
        char *json = cheat_load_doc(file, &non_json);
        if (!json) { send_json(fd, "{\"ok\":false,\"error\":\"cannot read file\"}"); return; }
        char title[160] = {0}, id[32] = {0}, ver[32] = {0}, proc[64] = {0};
        json_str_after(json, "name", title, sizeof(title));
        json_str_after(json, "id", id, sizeof(id));
        json_str_after(json, "version", ver, sizeof(ver));
        json_str_after(json, "process", proc, sizeof(proc));
        const char *fmt = path_ext_is(file, ".shn") ? "shn"
                        : path_ext_is(file, ".mc4") ? "mc4" : "json";
        cheat_entry_t *ents = (cheat_entry_t *)malloc(CHEAT_ENTS_BYTES);
        size_t OUTSZ = 65536;
        char *out = (char *)malloc(OUTSZ);
        if (!out || !ents) { free(out); free(ents); free(json); send_json(fd, "{\"ok\":false}"); return; }
        char et[200], ei[64], ev[64], ep[128];
        json_escape(title, et, sizeof(et)); json_escape(id, ei, sizeof(ei));
        json_escape(ver, ev, sizeof(ev));   json_escape(proc, ep, sizeof(ep));
        size_t len = 0;
        len += snprintf(out + len, OUTSZ - len,
                        "{\"ok\":true,\"title\":\"%s\",\"id\":\"%s\",\"version\":\"%s\","
                        "\"process\":\"%s\",\"format\":\"%s\",\"mods\":[", et, ei, ev, ep, fmt);
        const char *from = mods_array_start(json);
        for (int i = 0; from && i < CHEAT_MAX_MODS && len < OUTSZ - 1024; i++) {
            const char *end = NULL;
            const char *blk = next_mod_block(from, &end);
            if (!blk) break;
            from = end;
            char nm[240] = {0};
            json_str_after_lim(blk, end, "name", nm, sizeof(nm));   /* this block's name */
            int dropped = 0;
            int n = parse_mod_entries_ex(blk, end, ents, CHEAT_MAX_ENTRIES, &dropped);
            char esc2[500]; json_escape(nm, esc2, sizeof(esc2));
            const char *stt = (lpid > 0) ? cheat_mod_state_blk(json, blk, end, (pid_t)lpid, lbase,
                                                               non_json)
                                         : "unknown";
            len += snprintf(out + len, OUTSZ - len,
                            "%s{\"index\":%d,\"name\":\"%s\",\"entries\":%d,\"dropped\":%d,"
                            "\"state\":\"%s\",\"on\":%s}",
                            i ? "," : "", i, esc2, n, dropped, stt,
                            strcmp(stt, "on") == 0 ? "true" : "false");
        }
        char idbuf[24] = {0};
        snprintf(idbuf, sizeof(idbuf), "%s", id);
        char vers[600] = "[]";
        if (idbuf[0]) cheat_versions_json(idbuf, vers, sizeof(vers));
        /* A MASTER CODE CHANGES WHAT TURNING EVERYTHING OFF MEANS - see cheat_master_info. */
        int m_rem = 0, m_has = cheat_master_info(json, &m_rem);
        snprintf(out + len, OUTSZ - len, "],\"versions\":%s,\"master\":%s,\"master_removable\":%s}",
                 vers, m_has ? "true" : "false", m_rem ? "true" : "false");
        send_json(fd, out);
        free(out); free(ents); free(json);
        return;
    }
    if (!strcmp(path, "/api/patch/apply") || !strcmp(path, "/api/patch/revert")) {
        char tid[24] = {0}, ib[16] = {0}, fb[8] = {0}, db[8] = {0};
        if (!qparam(rawpath, "title", tid, sizeof(tid))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need title\"}"); return;
        }
        qparam(rawpath, "index", ib, sizeof(ib));
        qparam(rawpath, "force", fb, sizeof(fb));
        qparam(rawpath, "dry", db, sizeof(db));
        char o[1400];
        ps4_patch_action_json(tid, ib[0] ? atoi(ib) : 0, fb[0] ? atoi(fb) : 0,
                              db[0] ? atoi(db) : 0,
                              !strcmp(path, "/api/patch/revert"), o, sizeof(o));
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheat/apply")) {
        /* /api/cheat/apply?file=&mod=&on=0|1&pid=&base=0x400000[&force=1][&name=] */
        char file[600] = {0}, mb[16] = {0}, ob[8] = {0}, pb[16] = {0}, bb[32] = {0},
             fb[8] = {0}, nm[160] = {0};
        if (!qparam(rawpath, "file", file, sizeof(file)) || !qparam(rawpath, "pid", pb, sizeof(pb))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need file and pid\"}"); return;
        }
        qparam(rawpath, "mod", mb, sizeof(mb)); qparam(rawpath, "on", ob, sizeof(ob));
        qparam(rawpath, "base", bb, sizeof(bb)); qparam(rawpath, "force", fb, sizeof(fb));
        qparam(rawpath, "name", nm, sizeof(nm));
        /* &check=1 RUNS THE WHOLE DECISION AND WRITES NOTHING - see cheat_apply_blk's check_only. */
        char ckb[8] = {0};
        qparam(rawpath, "check", ckb, sizeof(ckb));
        int check_only = ckb[0] ? atoi(ckb) : 0;
        int idx = mb[0] ? atoi(mb) : 0, want = ob[0] ? atoi(ob) : 1, force = fb[0] ? atoi(fb) : 0;
        pid_t pid = (pid_t)atoi(pb);
        /* Same order as /api/cheat/list, and for the same reason - see agent_image_base(). */
        intptr_t base = bb[0] ? (intptr_t)strtoull(bb, NULL, 0) : 0;
        if (!base) base = agent_image_base();
        if (!base) base = (intptr_t)PATCH_NO_ASLR;
        char detail[200] = {0};
        int rc = cheat_apply_mod(file, idx, want, pid, base, force, check_only,
                                 detail, sizeof(detail));
        char gtitle[24] = {0};
        running_title_id(gtitle, sizeof(gtitle));
        const char *what = nm[0] ? nm : "Cheat";
        /* NOTHING HAPPENED, SO NOTHING IS ANNOUNCED. A toast for a question is noise. */
        if (!check_only) cheat_result_toast(what, want, rc, detail, gtitle);
        char ed[300], out[1100];
        /* A SENTENCE FOR THE OWNER, alongside the numbers for us. Without it errText() in the page
           falls through to `detail` and toasts "entries=3 written=0 skipped=0 failed=3". It is left
           out entirely when there is nothing to explain, so a success carries no message at all. */
        char msg[420] = {0}, emsg[500];
        cheat_rc_message(rc, detail, want, msg, sizeof(msg));
        json_escape(detail, ed, sizeof(ed));
        json_escape(msg, emsg, sizeof(emsg));
        snprintf(out, sizeof(out),
                 "{\"ok\":%s,\"rc\":%d,\"mod\":%d,\"on\":%d,\"pid\":%d,\"base\":\"0x%llx\","
                 "\"detail\":\"%s\"%s%s%s}",
                 rc >= 0 ? "true" : "false", rc, idx, want, (int)pid,
                 (unsigned long long)base, ed,
                 msg[0] ? ",\"message\":\"" : "", msg[0] ? emsg : "", msg[0] ? "\"" : "");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/mem/find/status")) {
        char out[2048];
        sig_status_json(out, sizeof(out));
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/mem/find/cancel")) {
        sig_cancel();
        send_json(fd, "{\"ok\":true,\"cancelled\":true}");
        return;
    }
    if (!strcmp(path, "/api/mem/find")) {
        /* THE SIGNATURE SEARCH. Read-only, one at a time, in its own thread - see sig_start. This is
           the primitive that porting a cheat by signature, the mask patch lines and finding an address
           from scratch all need, and none of them can exist without it.
           Offsets are IMAGE-RELATIVE, like a cheat file's. */
        char pt[200] = {0}, pb[16] = {0}, bb[32] = {0}, f1[24] = {0}, f2[24] = {0};
        if (!qparam(rawpath, "pattern", pt, sizeof(pt)) || !pt[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"need a pattern, e.g. pattern=488B05????????89\"}");
            return;
        }
        qparam(rawpath, "pid", pb, sizeof(pb));
        qparam(rawpath, "base", bb, sizeof(bb));
        qparam(rawpath, "from", f1, sizeof(f1));
        qparam(rawpath, "to", f2, sizeof(f2));
        pid_t spid = (pid_t)atoi(pb);
        intptr_t sbase = bb[0] ? (intptr_t)strtoull(bb, NULL, 0) : 0;
        long long sfrom = f1[0] ? strtoll(f1, NULL, 0) : 0;
        long long sto = f2[0] ? strtoll(f2, NULL, 0) : 0;
                /* WHAT "THE WHOLE MODULE" MEANS HERE. The agent measures its own base and size and reports
           both, so neither is guessed; the no-ASLR address is the last resort it always was. */
        if (!spid || !sbase) {
            char rtid[24] = {0};
            pid_t apid = 0; intptr_t abase = 0;
            if (running_game(rtid, sizeof(rtid), &apid, &abase) == 1) {
                if (!spid) spid = apid;
                if (!sbase) sbase = abase;
            }
        }
        if (!sbase) sbase = agent_image_base();
        if (!sbase) sbase = (intptr_t)PATCH_NO_ASLR;
        if (sto <= 0) {
            long long sz = agent_image_size();
            sto = sz > 0 ? sz : (long long)(32 << 20);
        }
        int rc = sig_start(pt, spid, sbase, sfrom, sto);
        const char *why = rc == 0 ? "" :
                          rc == -1 ? "a search is already running - read /api/mem/find/status" :
                          rc == -2 ? "that is not a usable signature: at least 4 bytes, 3 of them real" :
                          rc == -3 ? "that range is empty, or wider than a single search will sweep" :
                          rc == -5 ? "there is no game running to search" :
                                     "could not start the search thread";
        char o[420], ew[300];
        json_escape(why, ew, sizeof(ew));
        snprintf(o, sizeof(o),
                 "{\"ok\":%s,\"rc\":%d,\"pid\":%d,\"base\":\"0x%llx\",\"from\":%lld,\"to\":%lld,"
                 "\"span_guessed\":%s%s%s%s}",
                 rc == 0 ? "true" : "false", rc, (int)spid, (unsigned long long)sbase, sfrom, sto,
                 agent_image_size() > 0 ? "false" : "true",
                 why[0] ? ",\"error\":\"" : "", why[0] ? ew : "", why[0] ? "\"" : "");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/mem/read")) {
        /* Read-only, and the one call that proves the in-game agent is working before any cheat
           is written. Same shape the PS5 answers with. */
        char pb[16] = {0}, ab[32] = {0}, lb[16] = {0};
        if (!qparam(rawpath, "pid", pb, sizeof(pb)) || !qparam(rawpath, "addr", ab, sizeof(ab))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need pid and addr\"}"); return;
        }
        qparam(rawpath, "len", lb, sizeof(lb));
        int pid = atoi(pb);
        intptr_t addr = (intptr_t)strtoull(ab, NULL, 0);
        int len = lb[0] ? atoi(lb) : 16;
        if (len < 1) len = 1;
        if (len > 256) len = 256;
        unsigned char buf[256];
        int rc = mem_read((pid_t)pid, addr, buf, (size_t)len);
        char hex[520]; hex[0] = 0;
        if (rc == 0) for (int i = 0; i < len; i++) snprintf(hex + i * 2, sizeof(hex) - i * 2, "%02X", buf[i]);
        char out[700];
        snprintf(out, sizeof(out),
                 "{\"ok\":%s,\"rc\":%d,\"pid\":%d,\"addr\":\"0x%llx\",\"len\":%d,\"hex\":\"%s\"}",
                 rc == 0 ? "true" : "false", rc, pid, (unsigned long long)addr, len, hex);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/mem/write")) {
        /* /api/mem/write?pid=&addr=&hex=[&expect=]
           THE EXPECT GATE IS THE WHOLE POINT, and it is the same one the cheat engine uses: write
           only if what is there now is what the caller says it should be. Without it this is a
           hand-loaded gun pointed at a running game. `expect` is optional only because the PS5's
           is; a caller that omits it is saying it has already read the bytes itself. */
        char pb[16] = {0}, ab[32] = {0}, hb[600] = {0}, eb[600] = {0};
        if (!qparam(rawpath, "pid", pb, sizeof(pb)) || !qparam(rawpath, "addr", ab, sizeof(ab)) ||
            !qparam(rawpath, "hex", hb, sizeof(hb))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need pid, addr and hex\"}"); return;
        }
        qparam(rawpath, "expect", eb, sizeof(eb));
        unsigned char want[256], expect[256], cur[256];
        int wl = hex2bytes(hb, want, sizeof(want));
        if (wl <= 0) { send_json(fd, "{\"ok\":false,\"error\":\"bad hex\"}"); return; }
        int el = eb[0] ? hex2bytes(eb, expect, sizeof(expect)) : 0;
        if (eb[0] && el != wl) {
            send_json(fd, "{\"ok\":false,\"error\":\"expect and hex are different lengths\"}");
            return;
        }
        pid_t pid = (pid_t)atoi(pb);
        intptr_t addr = (intptr_t)strtoull(ab, NULL, 0);
        if (!ADDR_OK(addr)) { send_json(fd, "{\"ok\":false,\"error\":\"address out of range\"}"); return; }
        if (el > 0) {
            if (mem_read(pid, addr, cur, (size_t)el) != 0) {
                send_json(fd, "{\"ok\":false,\"error\":\"cannot read that address\"}"); return;
            }
            if (memcmp(cur, expect, (size_t)el) != 0) {
                char gh[520]; gh[0] = 0;
                for (int i = 0; i < el; i++) snprintf(gh + i * 2, sizeof(gh) - i * 2, "%02X", cur[i]);
                char o[800];
                snprintf(o, sizeof(o),
                         "{\"ok\":false,\"error\":\"expect_mismatch\",\"addr\":\"0x%llx\","
                         "\"found\":\"%s\",\"message\":\"Nothing was written - that address "
                         "does not hold the bytes you expected.\"}",
                         (unsigned long long)addr, gh);
                send_json(fd, o);
                return;
            }
        }
        int rc = mem_write(pid, addr, want, (size_t)wl);
        char o[400];
        snprintf(o, sizeof(o),
                 "{\"ok\":%s,\"rc\":%d,\"pid\":%d,\"addr\":\"0x%llx\",\"len\":%d,"
                 "\"gated\":%s}",
                 rc == 0 ? "true" : "false", rc, (int)pid, (unsigned long long)addr, wl,
                 el > 0 ? "true" : "false");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheat/rescan") || !strcmp(path, "/api/cheats/rescan")) {
        /* The Settings panel's Rescan button, and the thing that makes an FTP drop useful. It used
           to hit the "not implemented on the PS4 yet" catch-all below. */
        mkdir(CHEAT_ROOT, 0777);
        mkdir(CHEAT_JSON_DIR, 0777);
        mkdir(CHEAT_SHN_DIR, 0777);
        mkdir(CHEAT_MC4_DIR, 0777);
        mkdir(CHEAT_PATCH_DIR, 0777);
        mkdir(CHEAT_INBOX_DIR, 0777);
        int filed = cheat_intake_all();
        titles_cache_drop();
        /* NEW CHEAT FILES CAN CHANGE WHO SHOULD BE ARMED - a title that had nothing a moment ago may
           have something now. Writes nothing when the answer is unchanged. */
        if (filed > 0) (void)agent_autoarm_reconcile();
        char o[400];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"platform\":\"ps4\",\"filed\":%d,\"drop_here\":\"%s\"}",
                 filed, CHEAT_INBOX_DIR);
        send_json(fd, o);
        notify_cheats_filed(filed);
        return;
    }
    if (!strcmp(path, "/api/cheat/library")) {
        /* How many of each kind are on this console - the one honest answer to "did my FTP copy
           actually land". The claim that the panel shows this in Settings has been removed: nothing
           in web/index.html fetches any library count, so it was describing UI that does not exist. */
        int nj = count_dir(CHEAT_JSON_DIR), nh = count_dir(CHEAT_SHN_DIR);
        int nm = count_dir(CHEAT_MC4_DIR), np = count_dir(CHEAT_PATCH_DIR);
        /* DERIVED, not asserted. "ready" was a literal, so a console with an entirely empty library
           reported the same status as a full one. Modelled on the PS5's g_lib_status. */
        const char *lib_status = (nj + nh + nm + np) ? "ready" : "empty";
        char o[700];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"platform\":\"ps4\",\"status\":\"%s\",\"json\":%d,"
                 "\"shn\":%d,\"mc4\":%d,\"patches\":%d,\"total\":%d,\"root\":\"%s\","
                 "\"drop_here\":\"%s\",\"shipped\":false}",
                 lib_status, nj, nh, nm, np, nj + nh + nm + np, CHEAT_ROOT, CHEAT_INBOX_DIR);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheat/running")) {
        /* Same shape the PS5 answers with, and the same keys the companion reads - see the note
           in tools about the one field that differs and why.

           THE TITLE DOES NOT DEPEND ON THE HELPER. running_title_id() asks the console (foreground
           app id, then a process walk) and is right whether or not our agent is in the game. The
           pid and base DO depend on it, because only code inside the process can report them, so
           they come back zero when the helper is not there and `can_cheat` says so plainly. */
        char tid[24] = {0};
        running_title_id(tid, sizeof(tid));
        if (!tid[0]) {
            send_json(fd, "{\"ok\":true,\"running\":false,\"platform\":\"ps4\"}");
            return;
        }

        /* The pid and image base come from the in-game agent, which only exists once the game has
           loaded it. When it is there, can_cheat is true and cheats can be toggled; when it is not,
           listing still works and can_cheat is false with `why` naming the reason the panel can
           act on. */
        pid_t pid = 0;
        intptr_t base = 0;
        char info[512];
        char agent_tid[16] = {0};
        int agrees = 1;
        int have_agent = (agent_info(info, sizeof(info)) == 0);
        if (have_agent) {
            const char *b = strstr(info, "\"base\":\"0x");
            const char *p = strstr(info, "\"pid\":");
            if (b) base = (intptr_t)strtoull(b + 10, NULL, 16);
            if (p) pid  = (pid_t)strtol(p + 6, NULL, 10);

            /* THIS ROUTE DID NOT COMPARE, and it is the one the panel decides can_cheat from. It
               took the title from the console and the pid and base from the agent and never asked
               whether they describe the same game - so an agent still answering from a game the
               owner has just closed would have reported the NEW title as ready to cheat, with the
               OLD game's pid and base. The first toggle then writes into whatever still holds that
               pid. running_game already refuses this; the route that gates the button did not.

               The reason reported stays "helper_not_loaded" rather than a new value: for the title
               being asked about, that is literally true - our helper is not in THAT game - and it
               keeps the panel's existing wording honest without inventing a state the UI has no
               sentence for. agent_title is added alongside so the disagreement is still visible to
               anyone reading the route. */
            if (agent_title_agrees(info, tid, agent_tid, sizeof(agent_tid)) == 0) {
                agrees = 0;
                have_agent = 0;
                pid = 0;
                base = 0;
            }
        }

        char ver[48] = {0};   /* 48: see the note on iver below - a version is a filename key */
        qparam(rawpath, "version", ver, sizeof(ver));
        if (!ver[0]) installed_ver_for(tid, ver, sizeof(ver));
        char pick[600]; const char *why = "none";
        int exact = cheat_pick_file(tid, ver, pick, sizeof(pick), &why);
        /* Why can't a cheat be written, if it can't: helper not loaded into this game yet, or an
           agent that answered without an image base. Empty when everything is ready. */
        const char *blocked = have_agent ? (base ? "" : "no_base") : "helper_not_loaded";
        char ef[700], o[1300], eat[40];
        json_escape(pick, ef, sizeof(ef));
        json_escape(agent_tid, eat, sizeof(eat));
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"running\":true,\"platform\":\"ps4\",\"title_id\":\"%s\","
                 "\"pid\":%d,\"base\":\"0x%llx\",\"cheat_file\":\"%s\",\"match\":\"%s\","
                 "\"exact\":%s,\"can_cheat\":%s,\"helper\":%s,\"why\":\"%s\","
                 "\"agent_title\":\"%s\",\"titles_agree\":%s}",
                 tid, (int)pid, (unsigned long long)base, ef, why,
                 exact ? "true" : "false",
                 (have_agent && base) ? "true" : "false",
                 have_agent ? "true" : "false", blocked,
                 eat, agrees ? "true" : "false");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/engine/agent")) {
        /* The real state of the load-safe agent, read from the console. `agent_present` is the one
           that matters most: it is true only when the agent's socket answers, i.e. it is actually
           inside a running game and ready. `installed` = the file is on the console; `listed`/
           `enabled` = named in plugins.ini ([default] or a per-title section). */
        char info[512];
        int rc = agent_info(info, sizeof(info));
        char rt[24] = {0};
        running_title_id(rt, sizeof(rt));
        struct stat pst;
        int installed = (stat(PMS_AGENT_PRX, &pst) == 0 && pst.st_size > 0);
        /* WHICH SECTIONS ARM IT, not just whether something does. A per-title arm and an
           every-game arm have very different blast radii, and "enabled":true said nothing about
           which one is in force - so the one instrument for "is this module wired into a game"
           could not answer the question anybody actually asks of it. */
        agent_arm_t arm;
        agent_scan_arm(&arm);
        int en = (arm.armed_default || arm.armed_titles) ? 1 : (arm.listed ? 0 : -1);
        char armt[200];
        json_escape(arm.titles, armt, sizeof(armt));
        int aa = autoarm_enabled();
        char o[1300];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"platform\":\"ps4\",\"agent_present\":%s,\"rc\":%d,"
                 "\"installed\":%s,\"enabled\":%s,\"listed\":%s,\"running_title\":\"%s\","
                 "\"armed_default\":%s,\"armed_titles\":%d,\"armed_for\":\"%s\","
                 "\"autoarm\":%s,"
                 "\"port\":%d,\"info\":%s,\"plugin\":\"%s\",\"config\":\"%s\"}",
                 rc == 0 ? "true" : "false", rc,
                 installed ? "true" : "false",
                 en == 1 ? "true" : "false",
                 en >= 0 ? "true" : "false",
                 rt,
                 arm.armed_default ? "true" : "false", arm.armed_titles, armt,
                 aa ? "true" : "false",
                 AGENT_PORT,
                 (rc == 0 && info[0] == '{') ? info : "null",
                 PMS_AGENT_PRX, GH_PLUGINS_INI);
        send_json(fd, o);
        return;
    }
    if (!strncmp(path, "/api/mods/", 10)) {
        const char *rest = path + 10;
        char tid[24] = {0};
        size_t ti = 0;
        while (rest[ti] && rest[ti] != '/' && ti < sizeof(tid) - 1) { tid[ti] = rest[ti]; ti++; }
        tid[ti] = 0;
        for (char *c = tid; *c; c++) {
            if (!((*c >= 'A' && *c <= 'Z') || (*c >= 'a' && *c <= 'z') ||
                  (*c >= '0' && *c <= '9') || *c == '-' || *c == '_')) { *c = 0; break; }
        }
        if (!tid[0]) { send_json(fd, "{\"ok\":false,\"reachable\":true,\"error\":\"bad title\"}"); return; }

        /* TWO VARIABLES, AND THE DISTINCTION IS LOAD-BEARING. `iver` is what is actually installed
           on this console: it feeds installed_version, the compatible test and patches_json, and none
           of those may report what the caller ASKED for as though it were a fact about the console.
           `pver` is the version the panel's picker selected, and all it does is choose which cheat
           file to read. Collapsing the two is exactly how /api/cheat/find came to report a requested
           version as the installed one. */
        char iver[48] = {0};
        installed_ver_for(tid, iver, sizeof(iver));
        char pver[48] = {0};
        if (!qparam(rawpath, "version", pver, sizeof(pver))) pver[0] = 0;

        /* ?state=1 - just the live on/off of each mod, which is the refresh the panel fires after
           every toggle. The page has always sent it and this console has always ignored it,
           composing the entire document (patches, the version list, more file reads) to answer a
           question about a handful of booleans. */
        char stq[8] = {0};
        int state_only = (qparam(rawpath, "state", stq, sizeof(stq)) && stq[0] && stq[0] != '0');

        char pick[600]; const char *why = "none";
        int exact = cheat_pick_file(tid, pver[0] ? pver : iver, pick, sizeof(pick), &why);
        /* A picked version the library has no file for is refused, not silently served from a
           neighbour: the caller is about to apply a mod by its index into what it was shown. */
        if (pver[0] && !exact) {
            char ev[110], o[600];
            json_escape(pver, ev, sizeof(ev));
            snprintf(o, sizeof(o),
                     "{\"ok\":false,\"reachable\":true,\"platform\":\"ps4\",\"title_id\":\"%s\","
                     "\"installed_version\":\"%s\",\"selected_version\":\"%s\","
                     "\"error\":\"no_cheat_file_for_version\",\"mods\":[],\"patches\":[],"
                     "\"candidates\":[]}", tid, iver, ev);
            send_json(fd, o);
            return;
        }
        if (!pick[0]) {
            /* No cheat file is not the same as nothing to show: a title can have game patches and
               no cheats at all, and returning an empty patches[] here hid them completely. */
            size_t PSZ = 32768;
            char *pj = (char *)malloc(PSZ);
            if (pj) patches_json(tid, iver, pj, PSZ);
            int has_patch = pj && pj[0] == '[' && pj[1] != ']';
            size_t OSZ = PSZ + 400;
            char *o = (char *)malloc(OSZ);
            if (!o) { free(pj); send_json(fd, "{\"ok\":false,\"reachable\":true}"); return; }
            snprintf(o, OSZ,
                     "{\"ok\":%s,\"reachable\":true,\"engine\":\"mutant\",\"platform\":\"ps4\","
                     "\"error\":\"%s\",\"title_id\":\"%s\",\"installed_version\":\"%s\","
                     "\"selected_version\":\"%s\","
                     "\"mods\":[],\"patches\":%s,\"candidates\":[]}",
                     has_patch ? "true" : "false",
                     has_patch ? "" : "no_local_cheat_found",
                     tid, iver, pver, pj ? pj : "[]");
            send_json(fd, o);
            free(o); free(pj);
            return;
        }

        char rtid[24] = {0};
        pid_t rpid = 0; intptr_t rbase = 0;
        int live = (running_game(rtid, sizeof(rtid), &rpid, &rbase) == 0 && !strcmp(rtid, tid));

        /* IS THE HELPER ALREADY SET UP FOR THIS TITLE? The panel cannot tell "you need to switch it
           on" from "it is on and will load when you start the game" without this, so it offered a
           button for something the app had already done. */
        int hv_autoarm = autoarm_enabled();
        int hv_armed = 0;
        {
            agent_arm_t aa;
            agent_scan_arm(&aa);
            if (aa.armed_default) hv_armed = 1;
            else if (aa.titles[0]) {
                const char *p2 = aa.titles;
                size_t tl = strlen(tid);
                while (*p2 && !hv_armed) {
                    const char *e2 = strchr(p2, ',');
                    size_t seg = e2 ? (size_t)(e2 - p2) : strlen(p2);
                    if (seg == tl && !strncmp(p2, tid, tl)) hv_armed = 1;
                    p2 = e2 ? e2 + 1 : p2 + seg;
                }
            }
        }

        int non_json = 0;
        char *doc = cheat_load_doc(pick, &non_json);
        if (!doc) { send_json(fd, "{\"ok\":false,\"reachable\":true,\"error\":\"cannot read cheat file\"}"); return; }
        char fver[32] = {0};
        json_str_after(doc, "version", fver, sizeof(fver));
        const char *fmt = path_ext_is(pick, ".shn") ? "shn"
                        : path_ext_is(pick, ".mc4") ? "mc4" : "json";
        /* COMPATIBLE WITH WHAT IS INSTALLED - not with what was asked for. `exact` is exact against
           the version in the REQUEST, so including it here meant that deliberately picking another
           version reported compatible:true and the panel painted it green with "matched", hiding the
           mismatch note entirely. The question this field answers is whether the cheat file suits the
           game on the console, and only iver can answer that. */
        int compatible = (!iver[0]) || (fver[0] && !strcmp(fver, iver));
        const char *reason = why;
        if (!iver[0] && !strcmp(why, "other version")) reason = "installed version unknown";

        cheat_entry_t *ents = (cheat_entry_t *)malloc(CHEAT_ENTS_BYTES);
        size_t OUTSZ = 65536;
        char *out = (char *)malloc(OUTSZ);
        if (!out || !ents) { free(out); free(ents); free(doc); send_json(fd, "{\"ok\":false}"); return; }
        const char *bn = strrchr(pick, '/');
        bn = bn ? bn + 1 : pick;
        char ep[700], eb[220];
        json_escape(pick, ep, sizeof(ep));
        json_escape(bn, eb, sizeof(eb));
        size_t len = 0;
        len += snprintf(out + len, OUTSZ - len,
            "{\"ok\":true,\"reachable\":true,\"engine\":\"mutant\",\"platform\":\"ps4\","
            "\"title_id\":\"%s\","
            "\"installed_version\":\"%s\",\"selected_version\":\"%s\","
            "\"file_version\":\"%s\",\"file\":\"%s\","
            "\"path\":\"%s\",\"format\":\"%s\",\"compatible\":%s,\"reason\":\"%s\","
            "\"running\":%s,\"pid\":%d,\"base\":\"0x%llx\","
            "\"helper_autoarm\":%s,\"helper_armed\":%s,\"mods\":[",
            tid, iver, pver, fver, eb, ep, fmt, compatible ? "true" : "false", reason,
            live ? "true" : "false", live ? (int)rpid : 0,
            (unsigned long long)(live ? rbase : 0),
            hv_autoarm ? "true" : "false", hv_armed ? "true" : "false");
        const char *from = mods_array_start(doc);
        for (int i = 0; from && i < CHEAT_MAX_MODS && len < OUTSZ - 1024; i++) {
            const char *end = NULL;
            const char *blk = next_mod_block(from, &end);
            if (!blk) break;
            from = end;
            char nm[240] = {0};
            json_str_after_lim(blk, end, "name", nm, sizeof(nm));   /* this block's name */
            int dropped = 0;
            int n = parse_mod_entries_ex(blk, end, ents, CHEAT_MAX_ENTRIES, &dropped);
            char en[500]; json_escape(nm, en, sizeof(en));
            const char *stt = live ? cheat_mod_state_blk(doc, blk, end, rpid, rbase, non_json)
                                   : "unknown";
            len += snprintf(out + len, OUTSZ - len,
                "%s{\"index\":%d,\"name\":\"%s\",\"entries\":%d,\"dropped\":%d,\"state\":\"%s\","
                "\"on\":%s,\"conflict\":%s,\"can_toggle\":%s,\"conflicts_with\":[]}",
                i ? "," : "", i, en, n, dropped, stt,
                !strcmp(stt, "on") ? "true" : "false",
                !strcmp(stt, "partial") ? "true" : "false",
                live ? "true" : "false");
        }
        /* INITIALISED, because state_only skips the call that fills it and this is formatted with
           %s straight into the reply - uninitialised stack there would put garbage inside "versions"
           and JSON.parse would throw, blanking the panel. */
        char vers[600] = "[]";
        char *pj = NULL;
        if (!state_only) {
            cheat_versions_json(tid, vers, sizeof(vers));
            size_t left = (len + 800 < OUTSZ) ? (OUTSZ - len - 800) : 0;
            size_t PSZ = left > 24576 ? 24576 : left;
            pj = (PSZ > 64) ? (char *)malloc(PSZ) : NULL;
            if (pj) patches_json(tid, iver, pj, PSZ);
        }
        int mm_rem = 0, mm_has = cheat_master_info(doc, &mm_rem);   /* see cheat_master_info */
        snprintf(out + len, OUTSZ - len,
                 "],\"patches\":%s,\"candidates\":[],\"master\":%s,\"master_removable\":%s,"
                 "\"versions\":%s}",
                 pj ? pj : "[]", mm_has ? "true" : "false", mm_rem ? "true" : "false", vers);
        send_json(fd, out);
        free(pj); free(out); free(ents); free(doc);
        return;
    }
    if (!strcmp(path, "/api/cheat/paths") || !strcmp(path, "/api/cheats/paths")) {
        char o[1100];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"platform\":\"ps4\",\"ftp\":\"ftp://%s:%d\",\"ftp_port\":%d,"
                 "\"drop_here\":\"%s\",\"json\":\"%s\",\"shn\":\"%s\",\"mc4\":\"%s\","
                 "\"patches\":\"%s\"}",
                 lan_ip_str(), ftp_live_port(), ftp_live_port(), CHEAT_INBOX_DIR,
                 CHEAT_JSON_DIR, CHEAT_SHN_DIR, CHEAT_MC4_DIR, CHEAT_PATCH_DIR);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheat/find")) {
        char tid[24] = {0};
        qparam(rawpath, "title", tid, sizeof(tid));
        if (!tid[0]) { send_json(fd, "{\"ok\":false,\"error\":\"title required\"}"); return; }
        /* &version= IS HONOURED, and this route used to ignore it completely.
           A title often carries one cheat file per game version and they are NOT equal - Dark
           Souls II has 1 mod in its 01.00 file and 7 in its 01.02 one. The panel's version picker
           asks for a specific version; reading only the INSTALLED version here meant it always got
           the same file back, with `match` cheerfully reporting "exact version", so picking 01.02
           silently did nothing. The PS5's twin has always accepted this parameter. */
        /* TWO VARIABLES. `real` is what this console has installed; `iver` is the version to look
           the file up by, which is the request's when it names one. They were ONE variable, so asking
           for a version made the route report that version as installed - the request echoed back as
           a fact about the console, which is the kind of answer that makes a panel agree with itself
           and be wrong. */
        char real[48] = {0};
        installed_ver_for(tid, real, sizeof(real));
        char iver[48] = {0};
        if (!qparam(rawpath, "version", iver, sizeof(iver)) || !iver[0])
            snprintf(iver, sizeof(iver), "%s", real);
        char pick[600]; const char *why = "none";
        /* `match` and `exact`, not `reason`. The companion reads THOSE two by name to decide
           whether a file really fits the installed version (companion/server.py's mods handler);
           with a differently named key every PS4 answer read as "no reason given" and every cheat
           file looked like a guess. */
        int exact = cheat_pick_file(tid, iver, pick, sizeof(pick), &why);
        char ep[700], er[110], ei[110];
        json_escape(pick, ep, sizeof(ep));
        json_escape(real, er, sizeof(er));
        json_escape(iver, ei, sizeof(ei));
        char o[1000];
        snprintf(o, sizeof(o),
                 "{\"ok\":%s,\"platform\":\"ps4\",\"title_id\":\"%s\",\"installed_version\":\"%s\","
                 "\"asked_version\":\"%s\",\"cheat_file\":\"%s\",\"match\":\"%s\","
                 "\"reason\":\"%s\",\"exact\":%s}",
                 pick[0] ? "true" : "false", tid, er, ei, ep, why, why,
                 exact ? "true" : "false");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/patch/list")) {
        char tid[24] = {0};
        qparam(rawpath, "title", tid, sizeof(tid));
        if (!tid[0]) { send_json(fd, "{\"ok\":false,\"error\":\"title required\"}"); return; }
        /* &version= IS HONOURED. The companion has always sent the installed version it read
           from its own library, and ignoring it meant the compatible flag on every row was decided
           against whatever this console happened to think - which for a title whose APP_VER it
           cannot read is nothing at all. */
        /* Same split as /api/cheat/find above: what is installed, and what to evaluate against. */
        char real[48] = {0};
        installed_ver_for(tid, real, sizeof(real));
        char iver[48] = {0};
        if (!qparam(rawpath, "version", iver, sizeof(iver)) || !iver[0])
            snprintf(iver, sizeof(iver), "%s", real);
        char pfile[600] = {0};
        int havefile = (patch_file_for(tid, pfile, sizeof(pfile)) == 0);
        size_t PSZ = 32768;
        char *pj = (char *)malloc(PSZ);
        if (!pj) { send_json(fd, "{\"ok\":false,\"error\":\"out of memory\"}"); return; }
        patches_json(tid, iver, pj, PSZ);
        size_t OSZ = PSZ + 1200;
        char *o = (char *)malloc(OSZ);
        if (!o) { free(pj); send_json(fd, "{\"ok\":false}"); return; }
        char ef[700];
        json_escape(pfile, ef, sizeof(ef));
        /* ok REFLECTS WHETHER THERE IS A FILE. It was hardcoded true, so "this title has no game
           patches" and "here are its game patches" were the same answer with a different array. */
        char er2[110], ei2[110];
        json_escape(real, er2, sizeof(er2));
        json_escape(iver, ei2, sizeof(ei2));
        snprintf(o, OSZ, "{\"ok\":%s,\"platform\":\"ps4\",\"title_id\":\"%s\","
                 "\"installed_version\":\"%s\",\"asked_version\":\"%s\",\"file\":\"%s\","
                 "\"patches\":%s}",
                 havefile ? "true" : "false", tid, er2, ei2, ef, pj);
        send_json(fd, o);
        free(o); free(pj);
        return;
    }
    /* Whatever is left in these families is genuinely not implemented here yet, and says so
       rather than falling through to the bare {} an unknown /api/ path returns - which reads as
       "nothing to report" instead of "this console does not do that". */
    if (!strncmp(path, "/api/cheat", 10) || !strncmp(path, "/api/mods", 9) ||
        !strncmp(path, "/api/patch", 10) || !strncmp(path, "/api/mem", 8)) {
        send_json(fd, "{\"ok\":false,\"unsupported\":true,\"platform\":\"ps4\","
                      "\"error\":\"This particular cheat route is not implemented on the PS4 yet\"}");
        return;
    }
    if (!strcmp(path, "/api/move") || !strcmp(path, "/api/move/status") ||
        !strcmp(path, "/api/game/delete-backup") || !strcmp(path, "/api/game/delete")) {
        /* These act on a mounted backup container, which is a PS5 arrangement. A PS4 game is
           installed or it is not there, so there is nothing here to move or unmount. */
        send_json(fd, "{\"ok\":false,\"unsupported\":true,\"platform\":\"ps4\","
                      "\"error\":\"Game backups are a PS5 feature - a PS4 game is installed "
                      "or it is not there\"}");
        return;
    }
    if (!strcmp(path, "/api/payloads/autostart")) {
        send_json(fd, "{\"ok\":false,\"unsupported\":true,\"platform\":\"ps4\","
                      "\"error\":\"Starting homebrew automatically is a PS5 feature\"}");
        return;
    }
    if (!strcmp(path, "/api/rest/prepare")) {
        /* On the PS5 this stops the payloads that crash the console on wake. The PS4 shop starts
           nothing and manages nothing, so there is genuinely nothing to stop - but the second
           sentence is worth saying, because it was measured: after a suspend and resume this
           console came back with the shop gone. */
        send_json(fd, "{\"ok\":true,\"platform\":\"ps4\",\"stopped\":[],\"survived\":[],"
                      "\"message\":\"Nothing of ours has to be stopped on the PS4 before rest "
                      "mode. The shop has to be loaded again after the console wakes.\"}");
        return;
    }
    /* ---------------------------------------------------- Payloads & Homebrews ---------------
     * The same two endpoints the PS5 answers, for the same reason: the catalogue is a static file
     * this ELF already carries, so the page reads that and this end answers only what is true right
     * now. Installing a seeded homebrew needs no route of its own - /api/install already takes a
     * path on this console.
     *
     * WHAT IS DIFFERENT HERE. There is no Payload Manager to ask which processes exist, so "is it
     * running" can only be a port that answered - and :9090 can never be one of those ports. A
     * payload with nothing to listen on therefore reports nothing, and the panel says so. */
    if (!strcmp(path, "/api/payloads")) {
        char live[400]; int ln = 0; live[0] = 0;
        for (int i = 0; i < PS4_PAYLOAD_BUNDLE_COUNT; i++) {
            const p4pb_entry_t *e = &PS4_PAYLOAD_BUNDLE[i];
            /* No Payload Manager on this console, so a port is all there is - and for a UDP
               service the only observable fact is that the port is taken. */
            int up = (e->port > 0) && (e->udp ? udp_port_taken(e->port) : p4_port_open(e->port));
            if (!up) continue;
            ln += snprintf(live + ln, sizeof(live) - (size_t)ln, "%s\"%s\"",
                           ln ? "," : "", e->filename);
            if (ln >= (int)sizeof(live) - 48) break;
        }
        char have[1400];
        hb_list_json(have, sizeof(have));
        /* SIZED FROM THE REAL COUNT, NOT A GUESS. This console reports 72 installed titles at
           about 12 bytes each, and a 900-byte buffer silently truncated the list - dropping the
           very homebrew that had just been installed, so the panel said "Install" about something
           that was already there. Room for ~250 titles. */
        /* WHAT WOULD ACTUALLY RUN, not what happens to be lying in the folder. /api/payloads/load
           resolves a request by stem against the BUNDLE, so the file it would start is
           PB_DIR/<bundled name> - and listing the directory instead reported leftovers from older
           builds under the same stem, one of which won the comparison and made a PC re-send a
           payload the console already had. Ask the bundle, stat one file per entry. */
        char have_p[1400]; int hp = 0; have_p[0] = 0;
        for (int i = 0; i < PS4_PAYLOAD_BUNDLE_COUNT && hp < (int)sizeof(have_p) - 90; i++) {
            char fp[600];
            snprintf(fp, sizeof(fp), "%s/%s", PB_DIR, PS4_PAYLOAD_BUNDLE[i].filename);
            struct stat ps;
            if (stat(fp, &ps) != 0 || !S_ISREG(ps.st_mode)) continue;
            /* AND WHICH RELEASE IT IS, when this lane fetched it.
               THIS CONSOLE WROTE THAT NOTE AND NEVER READ IT BACK. dl_take_note() has recorded
               the tag beside every download since the lane was written, and the reply left the
               field out - so the panel had nothing to compare against but the catalogue's
               version, which is the version the machine that BUILT the catalogue held. An update
               taken here was therefore offered again on every check, for ever. The PS5's copy of
               this reply has carried `v` all along; this one did not, and nothing said so. */
            char bv[160], ebv[200];
            dl_taken_version(PB_DIR, PS4_PAYLOAD_BUNDLE[i].filename, bv, sizeof(bv));
            json_escape(bv, ebv, sizeof(ebv));
            hp += snprintf(have_p + hp, sizeof(have_p) - (size_t)hp,
                           "%s{\"n\":\"%s\",\"s\":%lld,\"v\":\"%s\"}",
                           hp ? "," : "", PS4_PAYLOAD_BUNDLE[i].filename,
                           (long long)ps.st_size, ebv);
        }
        /* AND WHAT THIS CONSOLE DOWNLOADED ITSELF - every file in the payload folder this build
           carries no copy of.
           WITHOUT THIS A SELF-DOWNLOADED PAYLOAD IS INVISIBLE. The panel decides whether to offer
           Run from `here`, and `here` on this lane is this very list, so a payload the console
           fetched for itself arrived, was cleaned up correctly, and then had no button - the one
           outcome that makes the whole download lane look broken. The PS5 build grew the
           equivalent (pb_downloaded_find) and this one never did.
           The bundled ones are answered from the bundle above, which is the rule that stops a
           leftover from an older build winning a comparison. */
        {
            DIR *pd = opendir(PB_DIR);
            if (pd) {
                struct dirent *pe;
                while ((pe = readdir(pd)) && hp < (int)sizeof(have_p) - 90) {
                    if (pe->d_name[0] == '.') continue;        /* .part files and the tag notes */
                    size_t nl = strlen(pe->d_name);
                    if (nl < 5 || strcasecmp(pe->d_name + nl - 4, ".elf")) continue;
                    int bundled = 0;
                    for (int i = 0; i < PS4_PAYLOAD_BUNDLE_COUNT; i++)
                        if (!strcmp(pe->d_name, PS4_PAYLOAD_BUNDLE[i].filename)) { bundled = 1; break; }
                    if (bundled) continue;
                    char fp[700];
                    struct stat ps;
                    snprintf(fp, sizeof(fp), "%s/%s", PB_DIR, pe->d_name);
                    if (stat(fp, &ps) != 0 || !S_ISREG(ps.st_mode) || ps.st_size <= 0) continue;
                    char edn[320], dv[160], edv[200];
                    json_escape(pe->d_name, edn, sizeof(edn));
                    dl_taken_version(PB_DIR, pe->d_name, dv, sizeof(dv));
                    json_escape(dv, edv, sizeof(edv));
                    hp += snprintf(have_p + hp, sizeof(have_p) - (size_t)hp,
                                   "%s{\"n\":\"%s\",\"s\":%lld,\"v\":\"%s\"}",
                                   hp ? "," : "", edn, (long long)ps.st_size, edv);
                }
                closedir(pd);
            }
        }
        char apps[3200];
        app_ids_json(apps, sizeof(apps));
        /* AND WHICH RELEASE EACH APP FOLDER IS - see the same block in the PS5's server.c. A
           folder app has no package and no byte count, which are the two keys the `kept` and
           `have` lists use, so without this the panel has no way to learn an emulator's version
           from the console and compares the catalogue's instead. */
        char taken[700]; int tk = 0; taken[0] = 0;
        {
            static const char *VR[] = { "/data/homebrew", "/mnt/usb0/homebrew", NULL };
            for (int v = 0; VR[v] && tk < (int)sizeof(taken) - 80; v++) {
                DIR *vd = opendir(VR[v]);
                if (!vd) continue;
                struct dirent *ve;
                while ((ve = readdir(vd)) && tk < (int)sizeof(taken) - 80) {
                    if (ve->d_name[0] == '.' || strlen(ve->d_name) > 16) continue;
                    char tv[160], etv[200], etid[40];
                    dl_taken_version(VR[v], ve->d_name, tv, sizeof(tv));
                    if (!tv[0]) continue;
                    json_escape(tv, etv, sizeof(etv));
                    json_escape(ve->d_name, etid, sizeof(etid));
                    tk += snprintf(taken + tk, sizeof(taken) - (size_t)tk,
                                   "%s{\"t\":\"%s\",\"v\":\"%s\"}",
                                   tk ? "," : "", etid, etv);
                }
                closedir(vd);
            }
        }
        /* SIZED FROM ITS PARTS. have_p grew from 900 to 1400 when it started carrying a version
           per payload and the files this console downloaded itself, and snprintf truncates
           silently - which on this route means the page gets invalid JSON and the whole panel
           goes blank. */
        char out[8192];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"on_console\":true,\"platform\":\"PS4\",\"console\":\"%s\","
                 /* THE BUILD THAT IS ANSWERING - see the matching note in the PS5's server.c. Our
                    own tile showed the version the catalogue was baked with, which is the version
                    the owner's folder held when this ELF was BUILT, so it is one release behind by
                    construction and made the panel offer an update that was already installed. */
                 "\"shop_version\":\"" SHOP_VERSION "\","
                 "\"source_here\":false,\"items\":null,\"hb_dir\":\"%s\","
                 /* `bundled` and `status` were on the PS5's reply and not on this one, so anything
                    reading them saw null here and could not tell "this console carries none" from
                    "this console did not say". */
                 "\"bundled\":%d,\"status\":\"\","
                 "\"state\":{\"live\":[%s],\"kept\":[%s],\"apps\":[%s],\"have\":[%s],\"taken\":[%s]}}",
                 console_id(), HB_DIR, PS4_PAYLOAD_BUNDLE_COUNT, live, have, apps, have_p,
                 taken);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/payloads/upstream")) {
        /* What ONE project has released, and where this console can get the bytes. Read-only and
           instant: the GitHub call happens on the worker thread in github_lane.h, so a project
           this console has not asked about yet answers {"ok":true,"pending":true} and the page
           polls. See the note on the PS5's copy of this route - both answer the same shape, and
           the page does not ask which console it is talking to. */
        char repo[160] = {0}, fb[8] = {0};
        qparam(rawpath, "repo", repo, sizeof(repo));
        int force = qparam(rawpath, "force", fb, sizeof(fb)) ? atoi(fb) : 0;
        int ok_repo = repo[0] && strchr(repo, '/') && !strstr(repo, "..") &&
                      !strchr(repo, '?') && !strchr(repo, '#') && !strchr(repo, ' ') &&
                      !strchr(repo, '&');
        if (!ok_repo) {
            send_json(fd, "{\"ok\":false,\"error\":\"no project was named\"}");
            return;
        }
        upstream_t snap;
        int fresh;
        memset(&snap, 0, sizeof(snap));
        pthread_mutex_lock(&g_up_lock);
        upstream_t *e = up_find(repo);
        long long now = (long long)time(NULL);
        fresh = (e && e->at && (now - e->at) < UP_TTL_S && !force);
        if (fresh) {
            snap = *e;
        } else {
            /* A FORCED CHECK MUST ACTUALLY ASK. Clearing `fresh` here is not enough: the page
               polls for the answer WITHOUT force, so the next poll found this slot still inside
               its six hours and handed back the very answer the owner asked us to refresh.
               Zeroing the timestamp is what makes the poll wait for the new one. */
            if (force && e) e->at = 0;
            up_enqueue(repo);
        }
        int qn = g_up_qn;
        pthread_mutex_unlock(&g_up_lock);
        if (!fresh) {
            char o[200];
            snprintf(o, sizeof(o), "{\"ok\":true,\"pending\":true,\"queued\":%d}", qn);
            send_json(fd, o);
            return;
        }
        char et[260], ep[700], es[260], ee[420], er[340];
        char *o = (char *)malloc(12000);
        if (!o) { send_json(fd, "{\"ok\":false,\"error\":\"out of memory\"}"); return; }
        json_escape(snap.tag, et, sizeof(et));
        json_escape(snap.page, ep, sizeof(ep));
        json_escape(snap.stable_tag, es, sizeof(es));
        json_escape(snap.error, ee, sizeof(ee));
        json_escape(repo, er, sizeof(er));
        int n = snprintf(o, 12000,
                 "{\"ok\":%s,\"repo\":\"%s\",\"tag\":\"%s\",\"page\":\"%s\","
                 "\"published\":\"%s\",\"prerelease\":%s,\"stable_tag\":\"%s\","
                 "\"error\":\"%s\",\"age\":%lld,\"assets\":[",
                 snap.error[0] ? "false" : "true", er, et, ep, snap.published,
                 snap.prerelease ? "true" : "false", es, ee, now - snap.at);
        for (int i = 0; i < snap.na && n > 0 && n < 11000; i++) {
            char an[420], au[420];
            json_escape(snap.a[i].name, an, sizeof(an));
            json_escape(snap.a[i].url, au, sizeof(au));
            n += snprintf(o + n, (size_t)(12000 - n),
                          "%s{\"name\":\"%s\",\"url\":\"%s\",\"size\":%lld,\"digest\":\"%s\"}",
                          i ? "," : "", an, au, snap.a[i].size, snap.a[i].digest);
        }
        if (n > 0 && n < 11900) snprintf(o + n, (size_t)(12000 - n), "]}");
        else                    snprintf(o, 12000,
                                         "{\"ok\":false,\"error\":\"that release lists more files "
                                         "than this console can report\"}");
        send_json(fd, o);
        free(o);
        return;
    }
    if (!strcmp(path, "/api/download/status")) {
        pthread_mutex_lock(&g_dlj_lock);
        char en[420], em[640], o[1400];
        json_escape(g_dlj.name, en, sizeof(en));
        json_escape(g_dlj.msg, em, sizeof(em));
        int pct = (g_dlj.total > 0) ? (int)((g_dlj.done * 100) / g_dlj.total)
                                   : (!strcmp(g_dlj.state, "done") ? 100 : 0);
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"job_id\":%d,\"state\":\"%s\",\"pct\":%d,\"done\":%lld,"
                 "\"total\":%lld,\"name\":\"%s\",\"msg\":\"%s\"}",
                 g_dlj.id, g_dlj.state[0] ? g_dlj.state : "idle", pct, g_dlj.done, g_dlj.total,
                 en, em);
        pthread_mutex_unlock(&g_dlj_lock);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/download/cancel")) {
        pthread_mutex_lock(&g_dlj_lock);
        g_dlj.cancel = 1;
        pthread_mutex_unlock(&g_dlj_lock);
        send_json(fd, "{\"ok\":true}");
        return;
    }
    if (!strcmp(path, "/api/engine/fetch")) {
        /* GET /api/engine/fetch?url=&dest=&name=&size=&digest=&purpose=&stem_clean=
           Download a file and put it somewhere. The same request the PS5 build takes, so the page
           sends one shape to either console. */
        char url[1300] = {0}, dest[400] = {0}, name[400] = {0}, dgst[72] = {0};
        char purp[16] = {0}, sv[24] = {0}, scb[8] = {0}, rtag[120] = {0};
        if (!qparam(rawpath, "url", url, sizeof(url)) ||
            (strncmp(url, "http://", 7) && strncmp(url, "https://", 8))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need an http:// or https:// url\"}");
            return;
        }
        if (!qparam(rawpath, "dest", dest, sizeof(dest)) || dest[0] != '/') {
            send_json(fd, "{\"ok\":false,\"error\":\"need an absolute dest folder\"}");
            return;
        }
        if (!qparam(rawpath, "name", name, sizeof(name)) || !name[0]) {
            const char *tail = strrchr(url, '/');
            snprintf(name, sizeof(name), "%s", (tail && tail[1]) ? tail + 1 : "download.bin");
        }
        /* Refuse only what could escape the destination folder - the owner chose these names. */
        if (strchr(name, '/') || strstr(name, "..")) {
            send_json(fd, "{\"ok\":false,\"error\":\"that file name is not allowed\"}");
            return;
        }
        qparam(rawpath, "digest", dgst, sizeof(dgst));
        qparam(rawpath, "purpose", purp, sizeof(purp));
        qparam(rawpath, "tag", rtag, sizeof(rtag));
        /* THE ARCHIVE HALF, all of it optional and all of it from the page - the same request
           shape the PS5 build takes. See the note on dl_job_t in ps5-app/onconsole/server.c. */
        char arb[8] = {0}, aroot[400] = {0}, atid[32] = {0}, adata[400] = {0};
        int want_archive = qparam(rawpath, "archive", arb, sizeof(arb)) ? atoi(arb) : 0;
        if (want_archive) {
            qparam(rawpath, "root", aroot, sizeof(aroot));
            qparam(rawpath, "tid", atid, sizeof(atid));
            qparam(rawpath, "data", adata, sizeof(adata));
            /* AN ABSOLUTE ROOT OR NOTHING. This is where a whole app is about to be written; a
               relative path would resolve against whatever directory this process happens to be
               in, which is not a thing to find out afterwards. */
            if (aroot[0] != '/' || strstr(aroot, "..")) {
                send_json(fd, "{\"ok\":false,\"error\":\"need an absolute folder to unpack "
                              "into\"}");
                return;
            }
        }
        /* 64 hex characters or nothing: a malformed digest must not silently mean "do not
           check". */
        if (dgst[0]) {
            int okd = (strlen(dgst) == 64);
            for (const char *h = dgst; okd && *h; h++)
                okd = (*h >= '0' && *h <= '9') || (*h >= 'a' && *h <= 'f') ||
                      (*h >= 'A' && *h <= 'F');
            if (!okd) {
                send_json(fd, "{\"ok\":false,\"error\":\"that is not a sha256\"}");
                return;
            }
        }
        pthread_mutex_lock(&g_dlj_lock);
        int busy = g_dlj.active;
        pthread_mutex_unlock(&g_dlj_lock);
        if (busy) {
            send_json(fd, "{\"ok\":false,\"error\":\"a download is already running - let it "
                          "finish first\"}");
            return;
        }
        mkparents(dest);
        mkdir(dest, 0777);
        {
            /* IS THERE ROOM? `size` is what the caller expects to write; when it is absent the
               check is skipped rather than guessed at. 256 MB of headroom, because a filesystem
               at absolute zero is a different kind of bad. */
            long long wantb = qparam(rawpath, "size", sv, sizeof(sv)) ? atoll(sv) : 0;
            /* AN ARCHIVE NEEDS ROOM FOR WHAT IS INSIDE IT TOO - see the same note in the PS5
               build. The six real release archives expand 2.2x to 3.3x. */
            if (wantb > 0 && want_archive) wantb *= 3;
            struct statvfs vfs;
            if (wantb > 0 && statvfs(dest, &vfs) == 0) {
                long long freeb = (long long)vfs.f_bavail * (long long)vfs.f_frsize;
                if (freeb < wantb + (256LL << 20)) {
                    char ed[420], o[800];
                    json_escape(dest, ed, sizeof(ed));
                    snprintf(o, sizeof(o),
                             "{\"ok\":false,\"error\":\"Not enough room - %s needs %lld MB and %s "
                             "has %lld MB free.\",\"free\":%lld,\"need\":%lld}",
                             name, wantb >> 20, ed, freeb >> 20, freeb, wantb);
                    send_json(fd, o);
                    return;
                }
            }
        }
        {
            /* Prove we can write there BEFORE promising a transfer. */
            char probe[780];
            snprintf(probe, sizeof(probe), "%s/.pms-write-test", dest);
            int pf = open(probe, O_WRONLY | O_CREAT | O_TRUNC, 0777);
            if (pf < 0) {
                char ed[420], o[620];
                json_escape(dest, ed, sizeof(ed));
                snprintf(o, sizeof(o),
                         "{\"ok\":false,\"error\":\"Cannot write to %s\"}", ed);
                send_json(fd, o);
                return;
            }
            close(pf);
            unlink(probe);
        }
        pthread_mutex_lock(&g_dlj_lock);
        memset(&g_dlj, 0, sizeof(g_dlj));
        g_dlj.id = ++g_dlj_seq;
        g_dlj.active = 1;
        snprintf(g_dlj.state, sizeof(g_dlj.state), "downloading");
        snprintf(g_dlj.msg, sizeof(g_dlj.msg), "Starting");
        snprintf(g_dlj.name, sizeof(g_dlj.name), "%s", name);
        snprintf(g_dlj.digest, sizeof(g_dlj.digest), "%s", dgst);
        snprintf(g_dlj.purpose, sizeof(g_dlj.purpose), "%s", purp);
        snprintf(g_dlj.tag, sizeof(g_dlj.tag), "%s", rtag);
        g_dlj.stem_clean = qparam(rawpath, "stem_clean", scb, sizeof(scb)) ? atoi(scb) : 0;
        g_dlj.archive = want_archive;
        snprintf(g_dlj.ar_root, sizeof(g_dlj.ar_root), "%s", aroot);
        snprintf(g_dlj.ar_tid, sizeof(g_dlj.ar_tid), "%s", atid);
        snprintf(g_dlj.ar_data, sizeof(g_dlj.ar_data), "%s", adata);
        /* `path` carries the url across to the thread and then becomes the file's path, which
           keeps the record to one buffer per thing rather than two. */
        snprintf(g_dlj.path, sizeof(g_dlj.path), "%s", url);
        snprintf(g_dlj.final, sizeof(g_dlj.final), "%s/%s", dest, name);
        int jid = g_dlj.id;
        pthread_mutex_unlock(&g_dlj_lock);
        ilog("fetch: requested %s -> %s/%s", url, dest, name);
        pthread_t tid;
        pthread_attr_t fa;
        pthread_attr_init(&fa);
        pthread_attr_setstacksize(&fa, 512 * 1024);
        int frc = pthread_create(&tid, &fa, p4_dlj_thread, NULL);
        pthread_attr_destroy(&fa);
        if (frc != 0) {
            pthread_mutex_lock(&g_dlj_lock);
            g_dlj.active = 0;
            snprintf(g_dlj.state, sizeof(g_dlj.state), "error");
            pthread_mutex_unlock(&g_dlj_lock);
            send_json(fd, "{\"ok\":false,\"error\":\"could not start the download\"}");
            return;
        }
        pthread_detach(tid);
        char ef[780], o[1000];
        json_escape(g_dlj.final, ef, sizeof(ef));
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"started\":true,\"job_id\":%d,\"path\":\"%s\","
                 "\"note\":\"poll /api/download/status\"}", jid, ef);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/payloads/load")) {
        char want[256] = {0};
        /* rawpath, NOT req. qparam() takes the request PATH - every other caller in this file
           passes rawpath - and handing it the whole request text made it parse the query out of a
           line that still had " HTTP/1.1" on the end, so the name never matched anything and the
           console answered "this build does not carry that one" about a payload it was holding. */
        qparam(rawpath, "name", want, sizeof(want));
        char wstem[128];
        p4_stem(want, wstem, sizeof(wstem));
        for (int i = 0; i < PS4_PAYLOAD_BUNDLE_COUNT; i++) {
            const p4pb_entry_t *e = &PS4_PAYLOAD_BUNDLE[i];
            char estem[128];
            p4_stem(e->filename, estem, sizeof(estem));
            if (strcmp(estem, wstem)) continue;
            char full[512], why[160];
            snprintf(full, sizeof(full), "%s/%s", PB_DIR, e->filename);
            int rc = gh_send_payload(full, why, sizeof(why));
            char out[400];
            snprintf(out, sizeof(out), "{\"ok\":%s,\"message\":\"%s\"}",
                     rc == 0 ? "true" : "false", why);
            send_json(fd, out);
            return;
        }
        /* AND A PAYLOAD THIS CONSOLE DOWNLOADED ITSELF, which is not in the bundle and was
           therefore answered "This build does not carry that one" - about a file sitting in its
           own payload folder, which the panel had just offered a Run for. Matched by stem, the
           same way the bundled ones are, so a name with a version in it still finds its file. */
        {
            DIR *pd = opendir(PB_DIR);
            if (pd) {
                struct dirent *pe;
                while ((pe = readdir(pd))) {
                    if (pe->d_name[0] == '.') continue;
                    size_t nl = strlen(pe->d_name);
                    if (nl < 5 || strcasecmp(pe->d_name + nl - 4, ".elf")) continue;
                    char dstem[128];
                    p4_stem(pe->d_name, dstem, sizeof(dstem));
                    if (strcmp(dstem, wstem)) continue;
                    char full[700], why[160];
                    snprintf(full, sizeof(full), "%s/%s", PB_DIR, pe->d_name);
                    closedir(pd);
                    int rc = gh_send_payload(full, why, sizeof(why));
                    char out[400];
                    snprintf(out, sizeof(out), "{\"ok\":%s,\"message\":\"%s\"}",
                             rc == 0 ? "true" : "false", why);
                    send_json(fd, out);
                    return;
                }
                closedir(pd);
            }
        }
        send_json(fd, "{\"ok\":false,\"message\":\"This build does not carry that one.\"}");
        return;
    }
    if (!strcmp(path, "/api/tile/status")) {
        /* Is the shop on the console's home screen? The PS5 build answers the same question, and
           the page and the companion both ask it - a PS4 that could not answer would simply look
           like a console with no app, which is the very thing this is here to report.

           Proof is the app's own app.pkg, exactly as for a game: a title can be registered in
           app.db and have nothing behind it, and that is the state that reads as "installed" while
           being unlaunchable. THE PACKAGE IS NOT INSTALLED FROM HERE: it carries this payload, so a
           payload that carried it would contain a copy of itself and grow with every build. The PC
           installs it, down the ordinary install lane. */
        long long onDisk = installed_app_pkg(PS4_TILE_TID);
        int tickets = ticket_count_for(PS4_TILE_TID);
        /* IS IT THE RIGHT BUILD? Separate from both "installed" and "will it open", and the state
           this console spent a day in: the icon was present, had its launch ticket and opened -
           and was an older build than the one this ELF carries. Nothing was looking for that.
           This ELF tries to replace it and the download service refuses a package the console
           serves to itself (0x80991404, PlayStation Network being blocked - which is the
           configuration the shop asks for), and the direct lane cannot make a launch ticket, so
           replacing a working icon with it would be a downgrade. The PC has no such problem: the
           download service takes a package served from the PC. So the honest thing this end can
           do is SAY the copy is old and let the PC act on it.

           -1 means "cannot tell": the lite build carries no package to compare against, and a
           lite payload must never report "stale" and send the PC into a repair loop over a
           comparison it could not make. */
#ifdef PMS_LITE
        int stale = -1;
#else
        /* THE STAMP COUNTS HERE TOO, and leaving it out cost this console an install of its own
           icon every ten minutes. tile_bytes_match() cannot succeed on a firmware that re-wraps
           app.pkg as it installs it - the size matches and the bytes do not - so without the stamp
           this answer is "stale" for ever, and the PC's watchdog acts on it for ever. The console's
           own repair path has always obeyed one-attempt-per-build; this is the same rule, told to
           the only other thing that can act on it. */
        int stale = (onDisk > 0) ? ((tile_bytes_match() || tile_repair_tried()) ? 0 : 1) : -1;
#endif
        char out[420];
        /* `launchable` is reported separately from `registered` on purpose. A title can be installed,
           byte-correct and listed by the console and STILL refuse to open, because the console will
           not start a title BGFT has no task for. Anything that shows the icon's state needs to be
           able to tell those two apart. */
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"platform\":\"ps4\",\"title_id\":\"%s\",\"registered\":%s,"
                 "\"bytes\":%lld,\"tickets\":%d,\"launchable\":%s,\"stale\":%s,"
                 "\"carries_package\":%s,\"installed_by\":\"companion\"}",
                 PS4_TILE_TID, onDisk > 0 ? "true" : "false", onDisk, tickets,
                 (onDisk > 0 && tickets > 0) ? "true" : "false",
                 stale < 0 ? "null" : (stale ? "true" : "false"),
#ifdef PMS_LITE
                 "false"
#else
                 "true"
#endif
                 );
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/install/status")) {
        /* THE SAME SHAPE THE PS5 ANSWERS, because the companion's console-local lane polls this and
           nothing else. Without it that lane would have handed the package over, polled a route
           that does not exist, read {} as "not finished yet" and sat there for its full hour before
           reporting that the console never said anything - with the game installed the whole time.
           A route that exists on one payload and not the other is a silent dead button.

           `accepted_only` is false on purpose: unlike the PS5's path, this verdict is not "the
           installer took it", it is app.pkg on disk at the expected size, which is the proof the
           companion would otherwise go and gather for itself. */
        job_refresh();
        pthread_mutex_lock(&g_job_lock);
        int done = g_job.active && (!strcmp(g_job.state, "installed") || !strcmp(g_job.state, "error"));
        int act  = g_job.active && !strcmp(g_job.state, "downloading");
        int okf  = g_job.active && !strcmp(g_job.state, "installed");
        char en[400], em[500], o[1100];
        json_escape(g_job.name, en, sizeof(en));
        json_escape(g_job.msg, em, sizeof(em));
        pthread_mutex_unlock(&g_job_lock);
        snprintf(o, sizeof(o),
                 "{\"active\":%s,\"done\":%s,\"ok\":%s,\"accepted_only\":false,"
                 "\"name\":\"%s\",\"message\":\"%s\"}",
                 act ? "true" : "false", done ? "true" : "false", okf ? "true" : "false", en, em);
        send_json(fd, o);
        return;
    }
    /* Install a package the console already holds, with the console's own installer rather than its
       downloader - the lane that cannot be stopped by a PlayStation Network patch check. */
    if (!strcmp(path, "/api/engine/install-direct")) {
        char lp[1024] = {0};
        if (!qparam(rawpath, "path", lp, sizeof(lp)) || lp[0] != '/') {
            send_json(fd, "{\"ok\":false,\"error\":\"no package was named\"}");
            return;
        }
        char err[320] = {0};
        if (install_direct_start(lp, err, sizeof(err)) != 0) {
            char esc[400], out[600];
            json_escape(err, esc, sizeof(esc));
            snprintf(out, sizeof(out), "{\"ok\":false,\"queued\":false,\"error\":\"%s\"}", esc);
            send_json(fd, out);
            return;
        }
        pthread_mutex_lock(&g_job_lock);
        long long jid = g_job.job_id;
        char tid[16];
        snprintf(tid, sizeof(tid), "%s", g_job.tid);
        pthread_mutex_unlock(&g_job_lock);
        char out[300];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"queued\":true,\"direct\":true,\"job_id\":%lld,\"title_id\":\"%s\"}",
                 jid, tid);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/install-local")) {
        char lp[1024] = {0};
        if (!qparam(rawpath, "path", lp, sizeof(lp)) || lp[0] != '/') {
            send_json(fd, "{\"ok\":false,\"error\":\"no package was named\"}");
            return;
        }
        install_local_path(fd, lp);
        return;
    }
    /* ---------------- THE QUEUE, AS THE DOCK EXPECTS IT ----------------------------------------
     *
     * This build served no /api/queue at all, and handle_get answers an unknown /api/ path with
     * 200 and an empty object - so on a console with no PC the dock did not merely stay empty, it
     * actively said "Nothing is installing. Pick a game and press Install." while a multi-gigabyte
     * install was writing to the drive. That is the failure the PS5 build's own comment warns
     * about: indistinguishable from a hang, and what makes someone power-cycle a console mid-write.
     *
     * One slot, because the console installs one package at a time. The shape is the PS5's, with
     * one difference in our favour: BGFT reports real byte counts, so `pct` is a measurement here
     * rather than the PS5's honest -1. */
    if (!strcmp(path, "/api/queue")) {
        job_refresh();
        pthread_mutex_lock(&g_job_lock);
        int show = g_job.held || g_job.active;
        char en[400], em[400], o[1000];
        json_escape(g_job.name[0] ? g_job.name : g_job.tid, en, sizeof(en));
        json_escape(g_job.msg, em, sizeof(em));
        const char *st = g_job.held ? "held"
                       : !strcmp(g_job.state, "installed") ? "playable"
                       : !strcmp(g_job.state, "error") ? "error"
                       : g_job.active ? "transferring" : "queued";
        int pct = g_job.held ? 0
                : !strcmp(g_job.state, "installed") ? 100
                : !strcmp(g_job.state, "error") ? 0
                : (g_job.total > 0 ? (int)((g_job.done * 100) / g_job.total) : -1);
        if (pct > 100) pct = 100;
        if (show)
            snprintf(o, sizeof(o),
                     "{\"tasks\":[{\"id\":\"console-local\",\"name\":\"%s\",\"state\":\"%s\","
                     "\"pct\":%d,\"msg\":\"%s\",\"lane\":\"console-local\",\"kind\":\"base\","
                     "\"title_id\":\"%s\",\"done\":%lld,\"total\":%lld}]}",
                     en, st, pct, em, g_job.tid, g_job.done, g_job.total);
        else
            snprintf(o, sizeof(o), "{\"tasks\":[]}");
        pthread_mutex_unlock(&g_job_lock);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/queue/start")) {
        /* Release the held install. Everything needed to start it is already in the slot, and it
           is read out under the same lock that claims the row - two callers pressing Start at once
           used to both read "held" and both register a task for it. */
        job_claim_out q;
        long long qclaim = job_claim(JOB_CLAIM_HELD, &q);
        if (!qclaim) { send_json(fd, "{\"ok\":true,\"started\":0}"); return; }
        const char *uri = q.uri, *qcid = q.cid, *qtype = q.type, *qnm = q.name;
        long long qsize = q.size;
        char err[256] = {0};
        OrbisBgftTaskId task = BGFT_INVALID_TASK_ID;
        if (bgft_install_url(uri, qnm, qcid, qsize, qtype, err, sizeof(err), &task) != 0) {
            job_claim_abort(qclaim);
            char esc[300], out[420];
            json_escape(err, esc, sizeof(esc));
            snprintf(out, sizeof(out), "{\"ok\":false,\"started\":0,\"error\":\"%s\"}", esc);
            send_json(fd, out);
            return;
        }
        pthread_mutex_lock(&g_job_lock);
        if (!job_claim_is_mine_locked(qclaim)) {   /* taken from us - see install_local_pkg */
            pthread_mutex_unlock(&g_job_lock);
            bgft_release(task);
            send_json(fd, "{\"ok\":false,\"started\":0,"
                          "\"error\":\"That install was stopped before it started\"}");
            return;
        }
        g_job.held = 0; g_job.active = 1; g_job.task = task;
        g_job.job_id = now_ms(); g_job.started_ms = now_ms();
        job_set_kind_locked(qtype, qsize, qcid);      /* where to look, and what size to expect */
        job_baseline_locked();
        snprintf(g_job.state, sizeof(g_job.state), "downloading");
        snprintf(g_job.msg, sizeof(g_job.msg), "The PS4 is downloading and installing it");
        g_job_claim = 0;
        pthread_mutex_unlock(&g_job_lock);
        send_json(fd, "{\"ok\":true,\"started\":1}");
        return;
    }
    if (!strcmp(path, "/api/queue/clear")) {
        /* "Clear done" - only ever the finished row. Clearing the slot under a running install
           would throw away the verdict that says whether it worked. */
        pthread_mutex_lock(&g_job_lock);
        int running = g_job.active && strcmp(g_job.state, "installed") && strcmp(g_job.state, "error");
        if (!running) memset(&g_job, 0, sizeof(g_job));
        pthread_mutex_unlock(&g_job_lock);
        send_json(fd, running ? "{\"ok\":false,\"error\":\"That install is still running. "
                                "Let it finish, then this row will clear.\"}"
                              : "{\"ok\":true,\"cleared\":true}");
        return;
    }
    if (!strcmp(path, "/api/engine/job")) {
        job_refresh();
        pthread_mutex_lock(&g_job_lock);
        char en[400], em[400];
        json_escape(g_job.name, en, sizeof(en));
        json_escape(g_job.msg, em, sizeof(em));
        char out[1200];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"active\":%s,\"job_id\":%lld,\"task\":%d,\"title_id\":\"%s\","
                 "\"name\":\"%s\",\"done\":%lld,\"total\":%lld,\"state\":\"%s\",\"msg\":\"%s\","
                 "\"rc\":\"0x%08X\"}",
                 g_job.active ? "true" : "false", g_job.job_id, (int)g_job.task, g_job.tid,
                 en, g_job.done, g_job.total, g_job.state, em, g_job.rc);
        pthread_mutex_unlock(&g_job_lock);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/spawn-status")) {
        job_refresh();
        pthread_mutex_lock(&g_job_lock);
        int running = g_job.active && !strcmp(g_job.state, "downloading");
        int has_result = g_job.active && (!strcmp(g_job.state, "installed") || !strcmp(g_job.state, "error"));
        long long secs = g_job.started_ms ? (now_ms() - g_job.started_ms) / 1000 : 0;
        pthread_mutex_unlock(&g_job_lock);
        char out[260];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"busy\":%s,\"stale\":false,\"busy_for\":%lld,\"has_result\":%s}",
                 running ? "true" : "false", secs, has_result ? "true" : "false");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/spawn-result")) {
        job_refresh();
        pthread_mutex_lock(&g_job_lock);
        int done = g_job.active && (!strcmp(g_job.state, "installed") || !strcmp(g_job.state, "error"));
        int ok = g_job.active && !strcmp(g_job.state, "installed");
        char em[400];
        json_escape(g_job.msg, em, sizeof(em));
        char out[900];
        /* THE VERDICT IS ON THE HAND-OVER, NOT ON THE WHOLE INSTALL - the same contract the PS5
           build answers here, and the companion is built around it: it polls this for ninety
           seconds and then stops, while the queue that follows allows thirty-five minutes to prove
           the finish from the console's own files. Answering only when the download had completed
           made every install longer than ninety seconds - which is most of them - report "the
           console took this package but never reported back" while it was installing perfectly.
           `rc` is what the companion waits for, so a running job carries the rc the register and
           start calls actually returned: zero, because they succeeded. */
        if (!done && !g_job.active)
            snprintf(out, sizeof(out), "{\"ok\":false,\"pending\":true,\"state\":\"idle\"}");
        else if (!done)
            snprintf(out, sizeof(out),
                     "{\"ok\":true,\"rc\":\"0x00000000\",\"accepted\":true,\"state\":\"%s\","
                     "\"content_id\":\"%s\",\"uri\":\"%s\",\"via\":\"bgft\"}",
                     g_job.state, g_job.tid, g_job.uri);
        else
            snprintf(out, sizeof(out),
                     "{\"ok\":%s,\"rc\":\"0x%08X\",\"content_id\":\"%s\",\"uri\":\"%s\","
                     "\"via\":\"bgft\",\"msg\":\"%s\"}",
                     ok ? "true" : "false", g_job.rc, g_job.tid, g_job.uri, em);
        pthread_mutex_unlock(&g_job_lock);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/spawn-cleanup")) {
        job_refresh();                 /* same reason as install-spawn: do not read a stale state */
        pthread_mutex_lock(&g_job_lock);
        int done = g_job.active && (!strcmp(g_job.state, "installed") || !strcmp(g_job.state, "error"));
        OrbisBgftTaskId live = (g_job.active && !done) ? g_job.task : BGFT_INVALID_TASK_ID;
        if (done) g_job.active = 0;
        /* NOT WHILE A CLAIM IS OUTSTANDING. The sweep frees every task of ours except `keep`, and
           during a claim `keep` is BGFT_INVALID_TASK_ID - so a registration that completed a moment
           earlier, but has not committed its id yet, is a task this would free out from under the
           install that just started it. */
        int claiming = (g_job_claim != 0);
        pthread_mutex_unlock(&g_job_lock);
        /* The companion calls this between installs. Sweeping here is what clears tasks left by a
           payload reload or a crash, which no longer have a job to finish them. */
        int freed = claiming ? 0 : bgft_sweep_ours(live);
        char out[96];
        snprintf(out, sizeof(out), "{\"ok\":true,\"cleaned\":true,\"freed\":%d}", freed);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/cancel")) {
        pthread_mutex_lock(&g_job_lock);
        OrbisBgftTaskId t = g_job.task;
        int act = g_job.active;
        pthread_mutex_unlock(&g_job_lock);
        int rc = -1;
        /* THE SAME GUARD bgft_release() APPLIES ONE LINE DOWN. A direct install has no task at all
           and a claim has not been given one yet, so both hold BGFT_INVALID_TASK_ID - and this
           called stop with it. What the service does with an id it never issued is written down
           nowhere, and this payload is inside a shared system daemon: the answer is not to ask. */
        if (act && t != BGFT_INVALID_TASK_ID && bgft_stop_fn) rc = bgft_stop_fn(t);
        if (act) bgft_release(t);
        pthread_mutex_lock(&g_job_lock);
        /* A cancel that lands during a claim takes the slot from whoever claimed it; their commit
           sees this cleared and hands its task back instead of installing behind this. */
        g_job_claim = 0;
        if (act) { g_job.released = 1;
                   snprintf(g_job.state, sizeof(g_job.state), "error");
                   snprintf(g_job.msg, sizeof(g_job.msg), "Stopped from the shop"); }
        pthread_mutex_unlock(&g_job_lock);
        char out[160];
        snprintf(out, sizeof(out), "{\"ok\":%s,\"stopped\":%s}",
                 act ? "true" : "false", (rc == 0) ? "true" : "false");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/log")) {
        long len = 0;
        char *log = slurp(INSTALL_LOG, &len);
        if (!log) { send_status(fd, "200 OK", "text/plain", ""); return; }
        const char *tail = log;
        if (len > 60000) tail = log + (len - 60000);
        send_status(fd, "200 OK", "text/plain", tail);
        free(log);
        return;
    }

    /* ---- file API ---- */
    if (!strncmp(path, "/api/fs/", 8)) {
        char p[1024];
        if (!fs_param_path(rawpath, p, sizeof(p))) {
            send_status(fd, "400 Bad Request", "application/json",
                        "{\"ok\":false,\"error\":\"absolute ?path= required\"}");
            return;
        }
        const char *op = path + 8;
        if (!strncmp(op, "stat", 4)) { fs_send_stat(fd, p); return; }
        if (!strncmp(op, "list", 4)) { fs_send_list(fd, p); return; }
        if (!strncmp(op, "read", 4)) { fs_send_read_ranged(fd, p, req); return; }
        if (!strncmp(op, "mkdir", 5)) {
            char tmp[1100];
            snprintf(tmp, sizeof(tmp), "%s/", p);
            mkparents(tmp);
            mkdir(p, 0777);
            struct stat st;
            send_json(fd, (stat(p, &st) == 0 && S_ISDIR(st.st_mode))
                          ? "{\"ok\":true}" : "{\"ok\":false,\"error\":\"could not create it\"}");
            return;
        }
        /* THE ONE FILE OPERATION HERE THAT DESTROYS SOMETHING, so it is the one with a boundary -
           see fs_deletable_path(). Without this route every deploy and cleanup run left its probe
           file behind on the console: the tools write 4 KB through /api/fs/write, stat it, and then
           ask for it to be removed, and the asking fell through to "unknown file operation".
           unlink first and then rmdir, which is the order the PS5 uses. A folder with anything in
           it is left alone, deliberately: nothing in this project needs to remove a tree from a
           PS4, and a route that walks a directory is a route that can walk the wrong one. */
        if (!strcmp(op, "delete")) {
            if (!fs_deletable_path(p)) {
                send_status(fd, "403 Forbidden", "application/json",
                            "{\"ok\":false,\"error\":\"that path is outside the folder the shop "
                            "may delete from\"}");
                return;
            }
            int rc = unlink(p);
            if (rc != 0) rc = rmdir(p);
            send_json(fd, rc == 0 ? "{\"ok\":true}"
                                  : "{\"ok\":false,\"error\":\"could not remove it\"}");
            return;
        }
        send_json(fd, "{\"ok\":false,\"error\":\"unknown file operation\"}");
        return;
    }

    /* ---- peers / identity ---- */
    if (!strcmp(path, "/api/register-pc")) {
        char ip[24] = {0}, port[8] = {0}, name[64] = {0}, ver[16] = {0};
        qparam(rawpath, "ip", ip, sizeof(ip));
        qparam(rawpath, "port", port, sizeof(port));
        qparam(rawpath, "name", name, sizeof(name));
        qparam(rawpath, "version", ver, sizeof(ver));
        if (ip[0]) pc_register(ip, port[0] ? atoi(port) : 8710, name, ver);
        send_json(fd, "{\"ok\":true}");
        return;
    }
    if (!strcmp(path, "/api/companion") || !strcmp(path, "/api/pcs")) {
        /* FRESHEST FIRST, AND NOTHING THAT HAS GONE QUIET. The page takes the first of these that
           answers, so the order IS the decision - see pc_better() and PC_STALE_MS above. Copied out
           under the lock and sorted outside it: this is called from the page's poll and holding the
           registry lock across a sort is a lock held in the request path for no reason. */
        pcpeer_t pcs[PC_MAX];
        pthread_mutex_lock(&g_pcs_lock);
        memcpy(pcs, g_pcs, sizeof(pcs));
        pthread_mutex_unlock(&g_pcs_lock);

        long long now = now_ms();
        for (int i = 0; i < PC_MAX; i++)
            if (pcs[i].ip[0] && (now - pcs[i].last_ms) > PC_STALE_MS)
                pcs[i].ip[0] = 0;                    /* gone quiet - stop offering it */

        for (int a = 0; a < PC_MAX; a++)
            for (int b = a + 1; b < PC_MAX; b++)
                if (pc_better(&pcs[a], &pcs[b])) { pcpeer_t t = pcs[a]; pcs[a] = pcs[b]; pcs[b] = t; }

        char out[1400];
        size_t len = 0;
        len += (size_t)snprintf(out + len, sizeof(out) - len, "{\"urls\":[");
        int wrote = 0;
        char best[64] = {0};
        for (int i = 0; i < PC_MAX; i++) {
            if (!pcs[i].ip[0]) continue;
            char u[80];
            snprintf(u, sizeof(u), "http://%s:%d", pcs[i].ip, pcs[i].port);
            if (!best[0]) snprintf(best, sizeof(best), "%s", u);
            if (len + 120 >= sizeof(out)) break;
            len += (size_t)snprintf(out + len, sizeof(out) - len, "%s\"%s\"", wrote ? "," : "", u);
            wrote++;
        }
        snprintf(out + len, sizeof(out) - len, "],\"url\":\"%s\"}", best);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/open")) {
        /* Open the shop on the PS4's OWN screen. This is how the app reaches the television without
           a dashboard tile: one tap in the app here, or on a phone, and the console's browser opens
           the shop. ?url= overrides, which is how a companion points the console at its library.

           FOUR ARGUMENTS, and that is deliberate. The PS5 build calls this same export with four,
           proven on hardware; the published PS4 declaration takes two. Extra arguments in registers
           are harmless on this ABI, whereas passing too few would leave whatever was already in the
           remaining registers to be read as parameters - so four zeros is the form that is correct
           whichever of the two this firmware really has. */
        char url[300] = {0};
        if (!qparam(rawpath, "url", url, sizeof(url)) || !url[0])
            snprintf(url, sizeof(url), "http://%s:%d/", lan_ip_str(), (int)PORT);
        int (*launch)(const char *, int, int, int) =
            (int (*)(const char *, int, int, int))dlsym_any("sceSystemServiceLaunchWebBrowser");
        int lrc = launch ? launch(url, 0, 0, 0) : -1;
        /* The message is not a consolation prize - it carries the address, so a browser that refuses
           to open still leaves the user something they can type on a phone. */
        /* ONE MESSAGE CARRIES THE ADDRESS, AND ONLY WHEN IT IS USEFUL. The browser opening IS the
           answer, so repeating the address there just puts a second near-identical toast on screen
           right after the startup one - which is what an owner sees as the app talking to itself.
           The address appears only in the fallback, where they actually have to type it. */
        if (launch && lrc == 0)
            notify("Opening PKG MUTANT SHOP");
        else
            notifyf("PKG MUTANT SHOP is running\nOpen %s in any browser", url);
        char eu[340], o[560];
        json_escape(url, eu, sizeof(eu));
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"launched\":%s,\"notified\":true,\"available\":%s,"
                 "\"url\":\"%s\"}",
                 (launch && lrc == 0) ? "true" : "false", launch ? "true" : "false", eu);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/network")) {
        /* THE DEVICES CARD WAS BOTH INVENTING A MACHINE AND DENYING THE REAL ONES.
         *
         * this_pc was null and peers was always empty, so on a PS4-served page the card showed a
         * green light beside "this PC - 0 titles" (there is no PC; the reader is on a PS4) and
         * "No other PCs found yet", even while a PC was registered with this console and serving
         * the very library being browsed. The PS5 build fills both in, so the same card was right
         * on one console and wrong on the other.
         *
         * `this_pc` is THIS CONSOLE, which is what the card means by it on a console-served page -
         * the machine you are reading on. The peers are the PCs that have announced themselves,
         * with the same ten-minute staleness rule /api/companion applies, so a PC that has gone
         * quiet stops being listed here too rather than sitting there with a green light. */
        char o[1400];
        size_t l = 0;
        l += (size_t)snprintf(o + l, sizeof(o) - l,
                              "{\"ok\":true,\"this_pc\":{\"id\":\"console\",\"name\":\"This PS4\","
                              "\"lan_ip\":\"%s\",\"port\":%d,\"url\":\"http://%s:%d\","
                              "\"os\":\"PS4\",\"version\":\"" SHOP_VERSION "\",\"local\":true},"
                              "\"console\":{\"ip\":\"%s\",\"name\":\"PS4\",\"online\":true,"
                              "\"platform\":\"ps4\"},\"peers\":[",
                              lan_ip_str(), PORT, lan_ip_str(), PORT, lan_ip_str());
        pcpeer_t pcs[PC_MAX];
        pthread_mutex_lock(&g_pcs_lock);
        memcpy(pcs, g_pcs, sizeof(pcs));
        pthread_mutex_unlock(&g_pcs_lock);
        long long now = now_ms();
        int first = 1;
        for (int i = 0; i < PC_MAX && l < sizeof(o) - 220; i++) {
            if (!pcs[i].ip[0]) continue;
            if ((now - pcs[i].last_ms) > PC_STALE_MS) continue;
            l += (size_t)snprintf(o + l, sizeof(o) - l,
                                  "%s{\"id\":\"%s\",\"name\":\"%s\",\"lan_ip\":\"%s\","
                                  "\"url\":\"http://%s:%d\",\"online\":true,\"os\":\"PC\"}",
                                  first ? "" : ",", pcs[i].ip,
                                  pcs[i].name[0] ? pcs[i].name : pcs[i].ip, pcs[i].ip,
                                  pcs[i].ip, pcs[i].port);
            first = 0;
        }
        snprintf(o + l, sizeof(o) - l, "]}");
        send_json(fd, o);
        return;
    }
    /* "SAVED" WAS A GREEN LIE. Settings posts its form and then reads the answer; the PS5 build
       replies {"saved":false,"on_console":true}, which is what makes the page paint its read-only
       banner and leave the fields alone. This build had no such route, so handle_get fell through
       to 200 {} - no `saved`, no `on_console` - and the page took that for a successful save,
       showed a green tick, and then the next poll refilled every field from a config that had
       never changed. Nothing is lost, but the app said it had done something it had not.
       A console genuinely cannot save these: they live in the PC's config.json. Saying so is the
       whole job. */
    if (!strcmp(path, "/api/config")) {
        send_json(fd, "{\"ok\":true,\"saved\":false,\"on_console\":true,\"platform\":\"ps4\"}");
        return;
    }
    /* The header's source bar reads sources / best / local_paths, and with none of them present it
       printed "(none configured) · 0 sources" on a console that is serving the page it is printed
       on. With no PC there are no mirrors - the source IS this console, and its own drives. The
       helper flags are the ones a PS4 can honestly answer: FTP is ours and is up whenever we are;
       ShadowMount is a PS5 service and is stated as absent rather than left for the page to guess. */
    if (!strcmp(path, "/api/sources")) {
        char pcsrc[420] = {0};
        size_t pl = 0;
        /* AND NOT ONE THAT HAS GONE QUIET. This emits "ok":true per entry - a green light - and it
           was the only reader of g_pcs in this file without the ten-minute rule that /api/companion
           and /api/devices both apply, so a PC registered once and closed months ago still sat in
           the header's source bar claiming to be up. */
        long long now_src = now_ms();
        pthread_mutex_lock(&g_pcs_lock);
        for (int i = 0; i < PC_MAX && pl < sizeof(pcsrc) - 90; i++) {
            if (!g_pcs[i].ip[0]) continue;
            if ((now_src - g_pcs[i].last_ms) > PC_STALE_MS) continue;
            pl += (size_t)snprintf(pcsrc + pl, sizeof(pcsrc) - pl,
                                   ",{\"name\":\"%s\",\"ok\":true,\"kind\":\"peer\"}",
                                   g_pcs[i].name[0] ? g_pcs[i].name : g_pcs[i].ip);
        }
        pthread_mutex_unlock(&g_pcs_lock);
        int fport = ftp_live_port();
        char o[1200];
        snprintf(o, sizeof(o),
                 "{\"sources\":[{\"name\":\"this PS4\",\"ok\":true,\"latency_ms\":0,"
                 "\"kind\":\"local\"}%s],\"best\":\"on-console\","
                 "\"local_paths\":[\"/user/app\",\"/mnt/usb0..usb7\"],"
                 "\"helpers\":{\"shadowmount\":false,\"shadowmount_port\":0,"
                 "\"ftp\":%s,\"ftp_port\":%d},"
                 "\"on_console\":true,\"platform\":\"ps4\"}",
                 pcsrc, fport ? "true" : "false", fport);
        send_json(fd, o);
        return;
    }
    /* WHAT IS RUNNING BESIDE THE SHOP ON THIS CONSOLE.
     *
     * The PS5 answers this because its backup mounter binds loopback-only and the PC therefore
     * cannot see it; the console is the only one that can. This build answered nothing at all - the
     * route fell through to the bare {} at the end of handle_get - and the companion overlays what
     * a console says only when the reply carries ok:true, so silence was read as "it did not say"
     * and the services panel had no answer about a PS4 at all. A missing route looks exactly like a
     * broken one.
     *
     * So it answers, and only about things this console really has. There is no backup mounter here
     * and none of the PS5's helper services, and shadowmount is stated as an explicit false rather
     * than left out, because a missing key and a false one read the same to a caller using .get();
     * the page has a PS4 branch that paints that row grey with a sentence instead of a red alarm.
     *
     * FTP IS PROBED, NOT DECLARED. It is the one thing named here that we do not ship: it belongs
     * to the jailbreak, the owner may never have started it, and the shop does not need it because
     * the companion moves files over our own file API on this port. Claiming somebody else's server
     * is up is how a panel ends up showing a green light for something that is not there. */
    if (!strcmp(path, "/api/helpers")) {
        int ftp = ftp_live_port();      /* the same one answer the other three give */
        char out[340];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"platform\":\"ps4\",\"shadowmount\":false,"
                 "\"shadowmount_port\":0,\"ftp\":%s,\"ftp_port\":%d,"
                 "\"shop\":true,\"shop_port\":%d,\"file_api\":true}",
                 ftp ? "true" : "false", ftp ? 2121 : 0, PORT);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/consoles")) {
        char out[300];
        snprintf(out, sizeof(out),
                 "{\"consoles\":[{\"id\":\"ps4\",\"name\":\"PS4\",\"ip\":\"%s\","
                 "\"platform\":\"ps4\",\"online\":true}]}", lan_ip_str());
        send_json(fd, out);
        return;
    }

    if (!strcmp(path, "/api/notify")) {
        char text[1200] = {0};
        if (qparam(rawpath, "text", text, sizeof(text)) && text[0]) notify(text);
        send_json(fd, "{\"ok\":true}");
        return;
    }
    if (!strcmp(path, "/api/quit")) {
        send_json(fd, "{\"ok\":true,\"bye\":true}");
        /* THE CONNECTION IS NOT CLOSED HERE. conn_thread owns this descriptor and closes it when it
           returns, so closing it as well was a DOUBLE CLOSE - and in this process that is not a
           harmless untidiness. GoldHEN injects every payload into the SAME host process, so an old
           instance and the new one that just asked it to stand down share one descriptor table:
           the second close can free a number the host, another thread, or the NEW INSTANCE'S
           LISTENING SOCKET has since been given.
           That is the shape of a failure measured here - 28,000 consecutive accept() failures with
           the port still open, every browser request answered by a reset, and no way to reload
           because the loader had gone. The reply is self-delimiting (Content-Length and
           Connection: close), so nothing is lost by letting the one owner close it. */
        ilog("quit: handing :%d over", PORT);
        /* NEVER _exit() HERE. The PS5 build can, because its payload owns its process. GoldHEN
           injects us INTO A SHARED HOST PROCESS (ScePartyDaemon), so _exit() tears down that whole
           process - including the newly loaded copy of ourselves that just asked us to stand down.
           Measured: the new build asked, the old build exited, and both disappeared, leaving :8710
           dead and the console with no shop at all.
           Instead: stop the accept loop and close the listening socket, which is all the new
           instance needs in order to bind. The host process is untouched.
           "OUR THREADS END" IS WHAT THIS USED TO CLAIM, AND IT WAS NOT TRUE. The watcher
           (startup_extras -> rt_thread) was `for (;;)` with no test of g_quit, so every reload left one
           behind walking the process list every two seconds inside this shared daemon, for ever. It
           tests the flag now. Any thread added here must do the same, or it leaks on every update. */
        g_quit = 1;
        int s = g_srv;
        g_srv = -1;
        if (s >= 0) { shutdown(s, SHUT_RDWR); close(s); }
        return;
    }

    if (!strcmp(path, "/") || path[0] == 0 || strncmp(path, "/api/", 5)) {
        serve_static(fd, path, req);
        return;
    }
    /* Unknown /api/ stays a quiet 200 {} - the shared page probes routes that only one console
       has, and an error here would paint a failure on a healthy console. */
    send_json(fd, "{}");
}

static void handle_post(int fd, const char *rawpath, const char *body) {
    char path[1024];
    snprintf(path, sizeof(path), "%s", rawpath);
    char *q = strchr(path, '?');
    if (q) *q = 0;

    /* THE DOCK'S PER-ROW BUTTONS. Without these the X, the retry arrow and the cancel cross were
       requests nothing answered - handle_post's fall-through sends 200 {}, which the page reads as
       a reply that simply did nothing. */
    if (!strncmp(path, "/api/queue/", 11) && strstr(path, "/cancel")) {
        pthread_mutex_lock(&g_job_lock);
        int held = g_job.held;
        if (held) memset(&g_job, 0, sizeof(g_job));
        pthread_mutex_unlock(&g_job_lock);
        if (held) { send_json(fd, "{\"ok\":true,\"cancelled\":true}"); return; }
        /* A running one is stopped the same way /api/engine/cancel stops it: tell BGFT to stop,
           hand the task back, and mark the slot. Written out rather than shared with that route
           because that one is a GET branch in another function; two short copies of six lines beat
           a refactor of the dispatch while the console is in the middle of a release. */
        pthread_mutex_lock(&g_job_lock);
        OrbisBgftTaskId qt = g_job.task;
        int qact = g_job.active;
        pthread_mutex_unlock(&g_job_lock);
        int qrc = -1;
        if (qact && qt != BGFT_INVALID_TASK_ID && bgft_stop_fn) qrc = bgft_stop_fn(qt);  /* see /api/engine/cancel */
        if (qact) bgft_release(qt);
        pthread_mutex_lock(&g_job_lock);
        g_job_claim = 0;        /* see /api/engine/cancel */
        if (qact) { g_job.released = 1;
                    snprintf(g_job.state, sizeof(g_job.state), "error");
                    snprintf(g_job.msg, sizeof(g_job.msg), "Stopped from the shop"); }
        pthread_mutex_unlock(&g_job_lock);
        char qo[160];
        snprintf(qo, sizeof(qo), "{\"ok\":%s,\"stopped\":%s}",
                 qact ? "true" : "false", (qrc == 0) ? "true" : "false");
        send_json(fd, qo);
        return;
    }
    if (!strncmp(path, "/api/queue/", 11) && strstr(path, "/dismiss")) {
        pthread_mutex_lock(&g_job_lock);
        int running = g_job.active && strcmp(g_job.state, "installed") && strcmp(g_job.state, "error");
        if (!running) memset(&g_job, 0, sizeof(g_job));
        pthread_mutex_unlock(&g_job_lock);
        send_json(fd, running ? "{\"ok\":false,\"error\":\"That install is still running. "
                                "Let it finish, then this row will clear.\"}"
                              : "{\"ok\":true,\"dismissed\":true}");
        return;
    }
    if (!strncmp(path, "/api/queue/", 11) && strstr(path, "/retry")) {
        /* The slot still holds the url from the attempt that failed, which is the whole point of
           retry on a console with no PC: there is nothing to re-send from anywhere. */
        job_claim_out rq;
        long long rclaim = job_claim(JOB_CLAIM_RETRY, &rq);
        if (!rclaim) {
            send_json(fd, "{\"ok\":false,\"error\":\"There is nothing here to try again\"}");
            return;
        }
        const char *uri = rq.uri, *rcid = rq.cid, *rtype = rq.type, *rnm = rq.name;
        long long rsize = rq.size;
        char err[256] = {0};
        OrbisBgftTaskId task = BGFT_INVALID_TASK_ID;
        if (bgft_install_url(uri, rnm, rcid, rsize, rtype, err, sizeof(err), &task) != 0) {
            job_claim_abort(rclaim);
            char esc[300], out[420];
            json_escape(err, esc, sizeof(esc));
            snprintf(out, sizeof(out), "{\"ok\":false,\"error\":\"%s\"}", esc);
            send_json(fd, out);
            return;
        }
        pthread_mutex_lock(&g_job_lock);
        if (!job_claim_is_mine_locked(rclaim)) {   /* taken from us - see install_local_pkg */
            pthread_mutex_unlock(&g_job_lock);
            bgft_release(task);
            send_json(fd, "{\"ok\":false,"
                          "\"error\":\"That install was stopped before it started\"}");
            return;
        }
        g_job.held = 0; g_job.active = 1; g_job.task = task;
        g_job.job_id = now_ms(); g_job.started_ms = now_ms();
        job_set_kind_locked(rtype, rsize, rcid);      /* the same two facts a retry also needs */
        job_baseline_locked();
        snprintf(g_job.state, sizeof(g_job.state), "downloading");
        snprintf(g_job.msg, sizeof(g_job.msg), "The PS4 is downloading and installing it");
        g_job_claim = 0;
        pthread_mutex_unlock(&g_job_lock);
        send_json(fd, "{\"ok\":true,\"retried\":true}");
        return;
    }
    if (!strcmp(path, "/api/install")) {
        /* The companion normally drives /api/engine/install-spawn; this exists so the on-console
           page can install too, and takes the same {"url": "..."} the PS5 build takes. */
        char uri[1024] = {0};
        const char *u = body ? strstr(body, "\"url\"") : NULL;
        if (u) {
            const char *c = strchr(u, ':');
            if (c) {
                const char *s1 = strchr(c, '"');
                if (s1) {
                    const char *s2 = strchr(s1 + 1, '"');
                    if (s2 && (size_t)(s2 - s1 - 1) < sizeof(uri)) {
                        memcpy(uri, s1 + 1, (size_t)(s2 - s1 - 1));
                        uri[s2 - s1 - 1] = 0;
                    }
                }
            }
        }
        /* The same {"install_key":"local:<path>"} / {"path":"<path>"} the PS5 build takes, so the
           page does not need to know which console it is talking to. */
        if (!uri[0]) {
            char lk[1200] = {0};
            json_str_field(body ? body : "", "install_key", lk, sizeof(lk));
            if (strncmp(lk, "local:", 6))
                json_str_field(body ? body : "", "path", lk, sizeof(lk));
            else
                memmove(lk, lk + 6, strlen(lk + 6) + 1);
            if (lk[0] == '/') { install_local_path(fd, lk); return; }
        }
        if (!uri[0]) { send_json(fd, "{\"ok\":false,\"error\":\"no package was named\"}"); return; }

        /* "+ QUEUE" MEANS LATER. This build ignored `mode` and installed on the spot, so the one
           button whose whole purpose is to postpone an install started one - and since
           install_local_pkg refuses a second while the first runs, every further queue press then
           answered with a red error on a console that was doing exactly what it was told. */
        char mode[24] = {0};
        json_str_field(body ? body : "", "mode", mode, sizeof(mode));
        char nm[160] = {0};
        json_str_field(body ? body : "", "name", nm, sizeof(nm));
        /* The companion and the page both send these; without them BGFT refuses the task. Falling
           back to the file name is what the old code did ALONE, and it is only ever right when the
           name happens to carry a full content id. */
        char cid[64] = {0}, ptype[8] = {0};
        json_str_field(body ? body : "", "content_id", cid, sizeof(cid));
        json_str_field(body ? body : "", "type", ptype, sizeof(ptype));
        long long psize = json_num_field(body ? body : "", "size");
        if (!cid[0]) pkg_content_id_from_url_name(uri, cid, sizeof(cid));
        /* ASK THE CONSOLE BEFORE REFUSING. Both busy checks below are only as true as the last
           poll: nothing in this process advances g_job.state on its own - it moves when a route
           reads it - so an install that finished while nobody was looking at the page leaves the
           slot still saying "downloading". Both guards then answer "An install is already running
           on this PS4" on a console that is doing nothing, and the guard that exists to stop a
           second install from orphaning the first becomes a lock-out that only a reload clears.
           Every other busy check in this file refreshes first (install_local_pkg, the direct lane,
           /api/engine/install-spawn); these two were the pair that did not. */
        job_refresh();
        if (!strcmp(mode, "queued")) {
            pthread_mutex_lock(&g_job_lock);
            int busy = g_job.active && strcmp(g_job.state, "installed") && strcmp(g_job.state, "error");
            if (!busy) {
                memset(&g_job, 0, sizeof(g_job));
                /* "NO TASK" IS -1 EVERYWHERE, NEVER 0. memset leaves task at 0, and 0 is a value
                   BGFT really does issue - the sweepers accept it, and observed ids are small
                   (00000075, 00000077). Only -1 is guarded, so a zeroed slot that ever reached a
                   stop call would be asking the service to stop somebody else's task. Nothing
                   reaches one today, because a held row is not active; this makes that safety a
                   property of the struct rather than of the routes that read it. */
                g_job.task = BGFT_INVALID_TASK_ID;
                g_job.held = 1;
                g_job.job_id = now_ms();
                snprintf(g_job.uri, sizeof(g_job.uri), "%s", uri);
                snprintf(g_job.name, sizeof(g_job.name), "%s", nm);
                snprintf(g_job.want_cid, sizeof(g_job.want_cid), "%s", cid);
                snprintf(g_job.want_type, sizeof(g_job.want_type), "%s", ptype);
                g_job.want_size = psize;
                pkg_content_id_from_url_name(uri, g_job.tid, sizeof(g_job.tid));
                snprintf(g_job.state, sizeof(g_job.state), "idle");
                snprintf(g_job.msg, sizeof(g_job.msg), "Waiting to start");
            }
            pthread_mutex_unlock(&g_job_lock);
            if (busy) {
                send_json(fd, "{\"ok\":false,\"busy\":true,"
                              "\"error\":\"An install is already running on this PS4\"}");
                return;
            }
            send_json(fd, "{\"ok\":true,\"queued\":true,\"held\":true,"
                          "\"message\":\"Added to the queue - press Start queue when you are ready\"}");
            return;
        }

        /* ONE INSTALL AT A TIME, and this route did not check. A second request overwrote the
           job slot wholesale, so the BGFT task the FIRST one started was orphaned: it carried on
           downloading while the shop's progress, its finished check and its cancel button all
           described the second package. install_local_pkg has had this guard for a while; the url
           branch never got it. The PS5 build refuses the second request outright, and so does
           this now - with the same sentence the local lane uses. */
        long long iclaim = job_claim(JOB_CLAIM_FRESH, NULL);
        if (!iclaim) {
            send_json(fd, "{\"ok\":false,\"busy\":true,"
                          "\"error\":\"An install is already running on this PS4\"}");
            return;
        }

        char err[256] = {0};
        OrbisBgftTaskId task = BGFT_INVALID_TASK_ID;
        /* The content id, the size and the package type, not four empty arguments. This call
           passed "" and 0 and then reported the console's refusal as if the package were at
           fault. */
        if (bgft_install_url(uri, nm, cid, psize, ptype, err, sizeof(err), &task) != 0) {
            job_claim_abort(iclaim);
            char esc[300], out[500];
            json_escape(err, esc, sizeof(esc));
            snprintf(out, sizeof(out), "{\"ok\":false,\"error\":\"%s\"}", esc);
            send_json(fd, out);
            return;
        }
        pthread_mutex_lock(&g_job_lock);
        if (!job_claim_is_mine_locked(iclaim)) {   /* taken from us - see install_local_pkg */
            pthread_mutex_unlock(&g_job_lock);
            bgft_release(task);
            send_json(fd, "{\"ok\":false,"
                          "\"error\":\"That install was stopped before it started\"}");
            return;
        }
        memset(&g_job, 0, sizeof(g_job));
        g_job.active = 1; g_job.task = task; g_job.job_id = now_ms(); g_job.started_ms = now_ms();
        g_job.expect = psize;
        snprintf(g_job.name, sizeof(g_job.name), "%s", nm);
        snprintf(g_job.uri, sizeof(g_job.uri), "%s", uri);
        pkg_content_id_from_url_name(uri, g_job.tid, sizeof(g_job.tid));
        /* THE CATEGORY AND THE BASELINE, which this one lane was starting without.
         *
         * Without a baseline g_job.base_had/base_size/base_mtime stay 0, and job_refresh's
         * `replaced` test is then true on the FIRST poll for any title that already has a file on
         * disk - so a reinstall, an update or an add-on flipped to "installed" while the download
         * was still running. A false success, of exactly the class this project has been caught by
         * before, and the reason the rule is that nothing counts as finished until app.pkg CHANGES.
         * Without the category, title_proof_facts also looks for the wrong file: an update proved
         * by the base game's app.pkg is a job that can never fail honestly.
         *
         * The other three lanes have done both since they were written; this one was missed
         * because it fills the slot by hand instead of sharing their block. */
        if (!strcmp(ptype, "PS4GP"))      snprintf(g_job.cat, sizeof(g_job.cat), "gp");
        else if (!strcmp(ptype, "PS4AC")) snprintf(g_job.cat, sizeof(g_job.cat), "ac");
        else if (!strcmp(ptype, "PS4GD")) snprintf(g_job.cat, sizeof(g_job.cat), "gd");
        job_baseline_locked();
        snprintf(g_job.state, sizeof(g_job.state), "downloading");
        g_job_claim = 0;
        pthread_mutex_unlock(&g_job_lock);
        send_json(fd, "{\"ok\":true,\"queued\":true}");
        return;
    }
    /* THE QUEUE BUTTONS ARE POSTs AND THEIR HANDLERS ARE GET BRANCHES, so Start queue and Clear
       both fell through to the 200 {} at the bottom of this function: two dead buttons on any page
       a PS4 serves, answering exactly like a success. Found while racing this route - the race test
       drives them with GET and passed, then the same two calls by POST did nothing at all. The PS5
       build delegates these the same way, for the same reason. */
    if (!strcmp(path, "/api/queue/start") || !strcmp(path, "/api/queue/clear")) {
        handle_get(fd, rawpath, NULL);
        return;
    }
    /* AND THE SAME TRAP, THIRD TIME: every /api/fs/ operation except write lives in the GET half,
       so a POST to /api/fs/delete or /api/fs/mkdir fell through to the bare 200 {} at the bottom
       of this function. Found while cleaning up after a test wrote a file to the console and then
       could not remove it - the delete answered {} and the caller read that as a success, which
       is exactly how the queue buttons and the Save button failed before it. Write is NOT here:
       it is answered further up, where the request body is still in hand. */
    if (!strncmp(path, "/api/fs/", 8) && strncmp(path, "/api/fs/write", 13)) {
        /* NULL, not `body`. handle_get's third argument is the RAW REQUEST TEXT - fs_send_read
           reads the Range header out of it - and handing it a POST body instead would have it
           looking for a header inside somebody's JSON. NULL is what "this caller has no request
           text" means everywhere else in this file. */
        handle_get(fd, rawpath, NULL);
        return;
    }
    /* THE SAVE BUTTON PAINTED GREEN AND SAVED NOTHING, and only the POST half of it. handle_get
       already answers /api/config with saved:false, which is what makes Settings show its
       read-only banner instead of live fields - but the page SAVES with a POST, and a POST for
       that path fell through to the 200 {} at the bottom of this function. No `saved`, no
       `on_console`: the page read that as a save that had worked, painted a green tick, and then
       the next poll refilled every field from a config that had never changed. These settings live
       in the PC's config.json and this console has nowhere to put them, so both verbs now answer
       the same sentence - which is the one the PS5 build answers too. */
    if (!strcmp(path, "/api/config")) {
        send_json(fd, "{\"ok\":true,\"saved\":false,\"on_console\":true,\"platform\":\"ps4\"}");
        return;
    }
    /* THE GAME PANEL'S TWO DESTRUCTIVE VERBS, IN WORDS RATHER THAN {}.
     *
     * The GET side of these has said "not on a PS4" for a while. The POST side - which is how the
     * page actually sends both of them - fell through to the bare {} at the end of this function,
     * and the two buttons read that reply differently and were both wrong for it. Delete tests
     * ok===false, so {} took the SUCCESS branch: the panel said the copy had been removed from the
     * console and reloaded the library, about a game nothing had touched. Move tests !ok, so it
     * showed its own fallback sentence and the owner was told the move could not be started with
     * no reason given.
     *
     * The page shows message-or-error out of the body, so the reason has to be IN the reply. It is
     * word for word the sentence the GET side already gives, because the same button must not get
     * two different answers depending on which verb carried it. */
    if (!strcmp(path, "/api/move") || !strcmp(path, "/api/game/delete") ||
        !strcmp(path, "/api/game/delete-backup")) {
        send_json(fd, "{\"ok\":false,\"unsupported\":true,\"platform\":\"ps4\","
                      "\"error\":\"Game backups are a PS5 feature - a PS4 game is installed "
                      "or it is not there\"}");
        return;
    }
    if (!strcmp(path, "/api/engine/agent")) {
        /* Switch the load-safe agent on or off.
             {"enabled":0}                      -> off everywhere: unlist and delete the file.
             {"enabled":1,"title":"CUSA01589"}  -> list for THAT ONE game only (a [TID] section).
                                                   The safe way to switch it on: no other game and
                                                   no other plugin the owner runs is touched, so if
                                                   anything is still wrong the blast radius is that
                                                   single title. This is what the first live test
                                                   uses. Takes effect the next time that game starts.
             {"enabled":1}                      -> list under [default] (every game). For after a
                                                   per-title test has proven it safe.
             {"enabled":1,"all_games":1}        -> the same thing, said out loud. Required, see below.
           A game already running does not pick this up - the loader reads plugins.ini at launch -
           so the message says to start the game. */

        /* THE KEY IS MANDATORY. It used to default to 1, so a POST with no body, an empty {}, or a
           misspelled {"enable":0} armed the helper for EVERY game - json_num_after returns its
           default for a NULL or empty body, an absent key, a literal null, and for any value that is
           not a bare number or true/false, including the STRING "0". The default is not moved to 0
           either: that would turn the same malformed POST into a silent agent_purge() and wipe a
           per-title arm somebody set up by hand. A sentinel is the only answer that cannot be wrong
           in one direction or the other. */
        /* {"autoarm":0|1} on its own switches the decision itself, without touching anything else.
           Off writes the marker and purges; on removes it and arms what should be armed. */
        long aa_l = json_num_after(body, "autoarm", -1);
        if (aa_l == 0 || aa_l == 1) {
            if (aa_l == 0) {
                int f = open(AUTOARM_OFF_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0777);
                if (f >= 0) { (void)!write(f, "off\n", 4); close(f); }
                int n2 = agent_purge();
                char o[420];
                snprintf(o, sizeof(o),
                         "{\"ok\":true,\"platform\":\"ps4\",\"autoarm\":false,\"removed\":%d,"
                         "\"message\":\"The helper will not switch itself on any more. It is off now "
                         "for every game.\"}", n2);
                send_json(fd, o);
            } else {
                unlink(AUTOARM_OFF_PATH);
                int armed = agent_autoarm_reconcile();
                char o[460];
                snprintf(o, sizeof(o),
                         "{\"ok\":%s,\"platform\":\"ps4\",\"autoarm\":true,\"armed\":%d,"
                         "\"message\":\"The helper will switch itself on for games that have cheats. "
                         "%d game%s ready - start one and cheats will work.\"}",
                         armed >= 0 ? "true" : "false", armed,
                         armed < 0 ? 0 : armed, armed == 1 ? "" : "s");
                send_json(fd, o);
            }
            return;
        }

        long want_l = json_num_after(body, "enabled", -1);
        if (want_l != 0 && want_l != 1) {
            send_json(fd, "{\"ok\":false,\"platform\":\"ps4\",\"error\":\"This request has to "
                          "say \\\"enabled\\\":0 or \\\"enabled\\\":1 - it is not guessed\"}");
            return;
        }
        int want = (int)want_l;
        if (!want) {
            int n = agent_purge();
            char o[420];
            snprintf(o, sizeof(o),
                     "{\"ok\":true,\"platform\":\"ps4\",\"enabled\":false,\"removed\":%d,"
                     "\"message\":\"The in-game helper is off and its file removed.\"}", n);
            send_json(fd, o);
            return;
        }
        char tid[24] = {0};
        json_str_after(body, "title", tid, sizeof(tid));
        int rc;
        char o[600];
        if (tid[0]) {
            rc = agent_enable_for_title(tid);
            snprintf(o, sizeof(o),
                     "{\"ok\":%s,\"platform\":\"ps4\",\"enabled\":%s,\"scope\":\"title\","
                     "\"title\":\"%s\",\"message\":\"%s\"}",
                     rc == 0 ? "true" : "false", rc == 0 ? "true" : "false", tid,
                     rc == 0 ? "The in-game helper is on for this game. Start it (or close and "
                               "reopen it) and the cheats panel will work."
                             : "The plugin list could not be written.");
        } else if ((int)json_num_after(body, "all_games", 0) != 1) {
            /* ASK FOR THIS ONE OUT LOUD. Without a title, this arms the helper inside every game the
               console runs, and that is not something to reach by leaving a field out. It gets its
               own sentence rather than falling through to "The plugin list could not be written.",
               which would be a false cause for a deliberate refusal. */
            send_json(fd, "{\"ok\":false,\"platform\":\"ps4\",\"scope\":\"default\","
                          "\"error\":\"This would switch the in-game helper on for every game. "
                          "Turn it on for one game first by naming its title, or say "
                          "\\\"all_games\\\":1 if that is really what you want\"}");
            return;
        } else {
            if (agent_deploy() == 0) rc = agent_set_enabled(1); else rc = -1;
            snprintf(o, sizeof(o),
                     "{\"ok\":%s,\"platform\":\"ps4\",\"enabled\":%s,\"scope\":\"default\","
                     "\"message\":\"%s\"}",
                     rc == 0 ? "true" : "false", rc == 0 ? "true" : "false",
                     rc == 0 ? "The in-game helper is on for all games. Start a game (or close and "
                               "reopen one) to use cheats."
                             : "The plugin list could not be written.");
        }
        send_json(fd, o);
        return;
    }
    if (!strncmp(path, "/api/mods/", 10)) {
        char tid[24] = {0};
        const char *rest = path + 10;
        size_t i = 0;
        while (rest[i] && rest[i] != '/' && i < sizeof(tid) - 1) { tid[i] = rest[i]; i++; }
        tid[i] = 0;
        const char *action = rest[i] == '/' ? rest + i + 1 : "";
        for (char *c = tid; *c; c++) {
            if (!((*c >= 'A' && *c <= 'Z') || (*c >= 'a' && *c <= 'z') ||
                  (*c >= '0' && *c <= '9') || *c == '-' || *c == '_')) { *c = 0; break; }
        }
        if (!tid[0]) { send_json(fd, "{\"ok\":false,\"error\":\"bad title\"}"); return; }

        /* Kept only so an older page's "select" call still gets a sane answer: this engine
           resolves the cheat file per request and has no document to select. */
        if (!strcmp(action, "select")) { send_json(fd, "{\"ok\":true,\"selected\":true}"); return; }

        /* A game PATCH is not a cheat - it comes from the XML library and is applied once rather
           than toggled - so it is handled BEFORE the cheat-file lookup. On the PS5 this ordering
           was load-bearing for a real bug: "apply" had been a silent alias for "toggle", which
           would flip an unrelated cheat the moment a title turned out to have patches. */
        if (!strcmp(action, "apply") || !strcmp(action, "unapply")) {
            char o[1400];
            ps4_patch_action_json(tid, (int)json_num_after(body, "index", 0),
                                  (int)json_num_after(body, "force", 0),
                                  (int)json_num_after(body, "dry", 0),
                                  !strcmp(action, "unapply"), o, sizeof(o));
            send_json(fd, o);
            return;
        }

        /* THE SAME FILE THE PANEL LISTED. A mod is applied by its INDEX, so if the page is showing
           one version's cheats and this resolves another version's file, index i is a different cheat
           - applied, and reported ok:true under the name the page thinks it pressed. The version
           travels with the request for exactly that reason, and a version the library cannot match
           exactly is refused rather than approximated. */
        char wver[48] = {0};
        json_str_after(body, "version", wver, sizeof(wver));
        char file[600];
        int frc = mods_file_for_req(tid, wver, file, sizeof(file));
        if (frc == -2) {
            char ev[110], o[520];
            json_escape(wver, ev, sizeof(ev));
            snprintf(o, sizeof(o),
                     "{\"ok\":false,\"platform\":\"ps4\",\"error\":\"no_cheat_file_for_version\","
                     "\"version\":\"%s\",\"message\":\"There is no cheat file for that version of "
                     "this game, so nothing was changed. Pick a version the list offers.\"}", ev);
            send_json(fd, o);
            return;
        }
        if (frc != 0) {
            send_json(fd, "{\"ok\":false,\"error\":\"no_local_cheat_found\"}");
            return;
        }
        char rtid[24] = {0};
        pid_t pid = 0; intptr_t base = 0;
        if (running_game(rtid, sizeof(rtid), &pid, &base) != 0 || strcmp(rtid, tid)) {
            /* TWO CAUSES, ONE SYMPTOM - AND THEY ARE TOLD APART HERE rather than described as a
               pair and left to the owner to work out which they have. running_game() answers no
               both when nothing is running and when the helper is not inside the game that is,
               but running_title_id() is a separate fact that does not need the helper at all.
               Saying "start the game first" to somebody looking at that game on their television
               is the kind of wrong answer that sends people hunting through cheat files. */
            char cur[24] = {0};
            running_title_id(cur, sizeof(cur));
            if (cur[0] && !strcmp(cur, tid)) {
                /* The game is running but the agent is not inside it - it was not listed before
                   this game launched. Enable the helper for this title in the app and start the
                   game again. */
                send_json(fd, "{\"ok\":false,\"error\":\"helper_not_loaded\",\"platform\":\"ps4\","
                              "\"why\":\"helper_not_loaded\",\"message\":\"Turn the in-game helper "
                              "on for this game in the app, then close the game and open it again "
                              "- cheats load with the game.\"}");
            } else if (cur[0]) {
                send_json(fd, "{\"ok\":false,\"error\":\"other_game_running\",\"platform\":\"ps4\","
                              "\"message\":\"A different game is running - cheats are written "
                              "into the memory of the game that is actually on screen.\"}");
            } else {
                send_json(fd, "{\"ok\":false,\"error\":\"game_not_running\",\"platform\":\"ps4\","
                              "\"message\":\"Start the game first - cheats are written into its "
                              "live memory.\"}");
            }
            return;
        }

        if (!strcmp(action, "disable-all")) {
            int non_json = 0;
            char *doc = cheat_load_doc(file, &non_json);
            if (!doc) { send_json(fd, "{\"ok\":false,\"error\":\"cannot read cheat file\"}"); return; }
            int reverted = 0, already = 0, failed = 0;
            const char *from = mods_array_start(doc);
            for (int m = 0; from && m < CHEAT_MAX_MODS; m++) {
                const char *end = NULL;
                const char *blk = next_mod_block(from, &end);
                if (!blk) break;
                from = end;
                char detail[200] = {0};
                int rc = cheat_apply_blk(doc, non_json, blk, end, m, 0, pid, base, 0, 0,
                                         detail, sizeof(detail));
                if (rc > 0) reverted++;
                else if (rc == 0) already++;
                else failed++;
            }
            /* AND THE MASTER CODE, LAST. Every mod that was patching the master's own routine has
               just been put back, so the routine itself is no longer needed. Only the Trainer form
               can go: it documents the bytes it replaced. The json form answers "the file does not
               say what was there before it" and stays until the game is closed, which is exactly what
               the panel tells that owner. */
            char mrem[120] = {0};
            int master_gone = cheat_master_off(doc, pid, base, non_json, mrem, sizeof(mrem));
            if (mrem[0]) ilog("cheats: %s", mrem);
            free(doc);
            char o[260];
            snprintf(o, sizeof(o),
                     "{\"ok\":true,\"disabled\":%d,\"already_off\":%d,\"failed\":%d,"
                     "\"master_removed\":%s}",
                     reverted, already, failed, master_gone > 0 ? "true" : "false");
            send_json(fd, o);
            if (failed)
                notifyf("Cheats turned off\n%d undone, %d would not undo - close the game to "
                        "clear those completely", reverted, failed);
            else if (reverted)
                notifyf("All cheats turned off\n%d change%s put back to the game's original code",
                        reverted, reverted == 1 ? "" : "s");
            else
                notify("No cheats were on\nNothing needed changing");
            return;
        }

        if (!strcmp(action, "toggle")) {
            int idx  = (int)json_num_after(body, "index", 0);
            int want = (int)json_num_after(body, "on", 1);
            int force = (int)json_num_after(body, "force", 0);
            char nm[200] = {0};
            json_str_after(body ? body : "", "name", nm, sizeof(nm));
            char detail[200] = {0};
            int rc = cheat_apply_mod(file, idx, want, pid, base, force, 0, detail, sizeof(detail));
            const char *what = nm[0] ? nm : "Cheat";
            cheat_result_toast(what, want, rc, detail, rtid);
            char ed[300], o[1100];
        /* A SENTENCE FOR THE OWNER, alongside the numbers for us. Without it errText() in the page
           falls through to `detail` and toasts "entries=3 written=0 skipped=0 failed=3". It is left
           out entirely when there is nothing to explain, so a success carries no message at all. */
            char msg[420] = {0}, emsg[500];
            cheat_rc_message(rc, detail, want, msg, sizeof(msg));
            json_escape(detail, ed, sizeof(ed));
            json_escape(msg, emsg, sizeof(emsg));
            snprintf(o, sizeof(o),
                     "{\"ok\":%s,\"rc\":%d,\"mod\":%d,\"on\":%d,\"pid\":%d,\"base\":\"0x%llx\","
                     "\"detail\":\"%s\"%s%s%s}",
                     rc >= 0 ? "true" : "false", rc, idx, want, (int)pid,
                     (unsigned long long)base, ed,
                     msg[0] ? ",\"message\":\"" : "", msg[0] ? emsg : "", msg[0] ? "\"" : "");
            send_json(fd, o);
            return;
        }
        send_json(fd, "{\"ok\":false,\"error\":\"unknown action\"}");
        return;
    }
    if (!strncmp(path, "/api/cheat", 10) || !strncmp(path, "/api/mods", 9) ||
        !strncmp(path, "/api/patch", 10)) {
        send_json(fd, "{\"ok\":false,\"unsupported\":true,\"platform\":\"ps4\","
                      "\"error\":\"This particular cheat route is not implemented on the PS4 yet\"}");
        return;
    }
    /* THE PANEL'S WRITE ROUTES LIVE ON THE COMPANION, AND SAYING SO IS BETTER THAN SAYING NOTHING.
       Download, send, seed and install all need the owner's folder, so neither console serves
       them. Measured: this build answered POST /api/payloads/get with "{}", which gives the page
       nothing to show but its generic "That did not work", while the PS5 build has answered
       "not supported on console" for a while - a sentence the page already handles.

       SCOPED TO THIS FAMILY ON PURPOSE. The blanket "{}" below is not a mistake in general:
       seventeen places in web/index.html treat an explicit ok:false differently from an empty
       object, and some call routes opportunistically that this console does not serve. Making the
       blanket answer explicit would turn silent fall-through into error toasts all over the app.
       /api/payloads/load and /api/payloads/autostart are implemented and returned long ago. */
    if (!strncmp(path, "/api/payloads/", 14)) {
        send_json(fd, "{\"ok\":false,\"error\":\"not supported on console\"}");
        return;
    }
    send_json(fd, "{}");
}

/* ------------------------------------------------------------ self-extraction
   Loading a new payload delivers the current UI with it, exactly like the PS5 build. */

static void extract_web(void) {
    mkdir("/data", 0777);
    mkdir(SHOP_DATA_DIR, 0777);
    mkdir(WEB_ROOT, 0777);
    int wrote = 0;
    for (int i = 0; i < WEB_FILES_COUNT; i++) {
        char full[600];
        snprintf(full, sizeof(full), "%s/%s", WEB_ROOT, WEB_FILES[i].path);
        mkparents(full);
        /* config.js is configuration, not build output: deploy writes the companion URL into it
           and overwriting it on every load would throw that away. */
        if (!strcmp(WEB_FILES[i].path, "config.js")) {
            struct stat cst;
            if (stat(full, &cst) == 0 && cst.st_size > 0) continue;
        }
        int fd = open(full, O_WRONLY | O_CREAT | O_TRUNC, 0644);
        if (fd < 0) continue;
        const unsigned char *d = WEB_FILES[i].data;
        unsigned int left = WEB_FILES[i].len;
        while (left > 0) {
            int w = (int)write(fd, d, left);
            if (w <= 0) break;
            d += w; left -= (unsigned)w;
        }
        close(fd);
        wrote++;
    }
    ilog("web: %d file(s) written to %s", wrote, WEB_ROOT);
}

/* ---------------------------------------------------------------------- main */

typedef struct { int fd; } conn_t;

/* ============================ WHO IS ALLOWED TO ASK ============================================
 *
 * THE PS4 BUILD HAD NONE OF THIS AND THE PS5 BUILD HAS HAD IT FOR A LONG TIME. Ported across
 * rather than invented: same helpers, same sentences, same rule.
 *
 * WHY IT MATTERS MORE HERE THAN IT SOUNDS. This server answers on every interface, as root, inside
 * a shared system daemon, and it sends "Access-Control-Allow-Origin: *" on its JSON. Without a
 * guard, ANY page the console's own browser happens to open - an ad frame on a game-wiki, a
 * shortened link, anything - can fire a state-changing GET at 127.0.0.1:8710 and READ the reply.
 * Not theoretical on this console: /api/quit takes the shop down, /api/notify puts arbitrary text
 * on the television, /api/engine/install-spawn takes a package URL of the caller's choosing, and
 * POST /api/fs/write writes any absolute path. The owner browses on this machine; that is the
 * whole point of the app.
 *
 * WHAT IT DOES NOT DO. A request with no Origin and no Referer is allowed: that is curl, the
 * companion, our own tools, and the console's own download service fetching /pkgfile/. The test is
 * only ever "if a page names itself, is that page from a private address".
 */
static int host_is_private(const char *h, size_t len) {
    char b[128];
    size_t j = 0;
    for (size_t i = 0; i < len && j < sizeof(b) - 1; i++) {
        if (h[i] == ':' || h[i] == '/') break;          /* port, or the path after the host */
        b[j++] = h[i];
    }
    b[j] = 0;
    if (!strcmp(b, "localhost") || !strcmp(b, "[::1]") || !strcmp(b, "::1")) return 1;
    unsigned a1, a2, a3, a4;
    int used = 0;
    /* %n, and the whole token must be the four octets. sscanf alone was satisfied by
       "10.0.0.1.attacker.example" or "10.0.0.1x" - any NAME that merely begins with a private
       address read as that address, which is exactly the thing this test exists to tell apart. */
    if (sscanf(b, "%u.%u.%u.%u%n", &a1, &a2, &a3, &a4, &used) != 4) return 0;   /* a NAME, not an IP */
    if (b[used] != 0) return 0;                                                  /* trailing junk */
    if (a1 > 255 || a2 > 255 || a3 > 255 || a4 > 255) return 0;
    if (a1 == 127 || a1 == 10) return 1;
    if (a1 == 192 && a2 == 168) return 1;
    if (a1 == 172 && a2 >= 16 && a2 <= 31) return 1;
    return 0;
}

static int request_origin_ok(const char *req) {
    static const char *HDRS[2] = { "\norigin:", "\nreferer:" };
    for (int k = 0; k < 2; k++) {
        const char *h = strcasestr_local(req, HDRS[k]);
        if (!h) continue;
        h += strlen(HDRS[k]);
        while (*h == ' ' || *h == '\t') h++;
        const char *end = h;
        while (*end && *end != '\r' && *end != '\n') end++;
        if (end - h == 4 && !strncmp(h, "null", 4)) continue;      /* sandboxed page: not a name */
        const char *hostp = h;
        const char *sep = strstr(h, "://");
        if (sep && sep < end) hostp = sep + 3;
        if (!host_is_private(hostp, (size_t)(end - hostp))) return 0;
    }
    return 1;
}

/* The browser's own statement of HOW a request was made.
 *
 * request_origin_ok() lets a request with no Origin and no Referer through on purpose. But a
 * browser can be made to send neither: a page with a no-referrer policy loading a state-changing
 * GET as an <img>, a <script> or a top-level navigation carries no Referer at all, and that walks
 * straight past the guard. Sec-Fetch-Mode and Sec-Fetch-Dest cannot be suppressed or forged by a
 * page, so on the routes that CHANGE something they are consulted as well. ABSENT headers are
 * allowed - older WebKit, curl and urllib send none - and everything our UI does is fetch(), so
 * nothing the app itself does changes. Read-only routes are not consulted at all. */
static int sec_fetch_ok(const char *req) {
    static const char *BAD_MODES[] = { "no-cors", "navigate", "nested-navigate", NULL };
    static const char *BAD_DESTS[] = { "image", "script", "style", "iframe", "frame", "object",
                                       "embed", "font", "video", "audio", NULL };
    static const char *HDRS[2] = { "\nsec-fetch-mode:", "\nsec-fetch-dest:" };
    for (int k = 0; k < 2; k++) {
        const char *h = strcasestr_local(req, HDRS[k]);
        if (!h) continue;
        h += strlen(HDRS[k]);
        while (*h == ' ' || *h == '\t') h++;
        const char *end = h;
        while (*end && *end != '\r' && *end != '\n' && *end != ' ' && *end != '\t') end++;
        size_t vl = (size_t)(end - h);
        const char **bad = k ? BAD_DESTS : BAD_MODES;
        for (int i = 0; bad[i]; i++) {
            size_t bl = strlen(bad[i]);
            if (vl != bl) continue;
            int same = 1;
            for (size_t j = 0; j < bl; j++) {
                char a = h[j];
                if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
                if (a != bad[i][j]) { same = 0; break; }
            }
            if (same) return 0;
        }
    }
    return 1;
}

/* The routes that change something on THIS console - the only ones sec_fetch_ok() is applied to.
   Re-derived from this file's own dispatch rather than copied from the PS5's list: the two builds
   do not serve the same routes. The stubs (/api/move, /api/game/delete*, /api/rest/prepare,
   /api/payloads/autostart) are here even though they currently change nothing, so that the day one
   of them grows a body it is already covered. */
static int route_changes_state(const char *rawpath, int is_post) {
    static const char *EXACT[] = {
        "/api/quit", "/api/notify", "/api/open", "/api/register-pc", "/api/install",
        "/api/payloads/load",
        /* /api/engine/fetch WRITES A FILE THE CONSOLE WILL LATER RUN, from a url in a query
           string - so an <img> on any page the owner opens could have aimed it at /data/payloads.
           It was missing from this list while the PS5's copy has carried it since the lane was
           written. It also now UNPACKS AN ARCHIVE into a folder of the caller's choosing, which
           makes the omission worse rather than better. */
        "/api/engine/fetch",
        "/api/engine/cancel", "/api/engine/spawn-cleanup", "/api/fs/write", "/api/fs/mkdir",
        "/api/fs/delete",
        /* The cheat engine's write side. /api/mods/<TID>/<action> is matched by prefix further
           down rather than listed here, because the title id is part of the path. */
        "/api/cheat/apply", "/api/patch/apply", "/api/patch/revert",
        /* The in-game helper's own switch - it edits GoldHEN's plugin list. */
        "/api/engine/agent",
        /* WRITES BYTES INTO A RUNNING GAME, and it is a GET whose arguments all live in the query
           string - so an <img> tag on any page the owner opens could fire it. request_origin_ok
           deliberately answers yes when both Origin and Referer are absent, which is exactly what a
           no-referrer navigation, a <script src> and <img referrerpolicy="no-referrer"> send, and
           sec_fetch_ok is only consulted for routes named here. It was the one state-changing route
           on this console outside the guard. The two rescan spellings rebuild the cheat library
           index, which is cheap but still a change somebody else's page should not be making. */
        "/api/mem/write", "/api/cheat/rescan", "/api/cheats/rescan",
        "/api/move", "/api/game/delete", "/api/game/delete-backup",
        "/api/rest/prepare", "/api/payloads/autostart",
        /* The queue's own controls: start releases a held install, clear throws a row away. */
        "/api/queue/start", "/api/queue/clear", NULL
    };
    char path[1024];
    snprintf(path, sizeof(path), "%s", rawpath);
    char *qs = strchr(path, '?');
    if (qs) *qs = 0;
    for (int i = 0; EXACT[i]; i++) if (!strcmp(path, EXACT[i])) return 1;
    /* install-spawn, install-url, install-direct, install-local: every one of them hands a package
       to the console. */
    if (!strncmp(path, "/api/engine/install-", 20)) return 1;
    /* The per-row POST verbs: cancel, dismiss, retry. Same rule the PS5 build uses. */
    if (is_post && !strncmp(path, "/api/queue", 10)) return 1;
    /* /api/mods/<TITLE_ID>/<toggle|disable-all|apply|unapply>: the title id sits in the middle of
       the path, so this cannot be an exact-match entry. Same rule the PS5 build uses. */
    if (is_post && !strncmp(path, "/api/mods/", 10)) return 1;
    return 0;
}

/* A LISTENING SOCKET FOR :PORT, with the options that matter, or -1.

   Factored out because the accept loop has to be able to build a SECOND one: a listener that has
   stopped accepting is not worth keeping, and the only way to replace it is to close it and bind
   again. The buffer sizes are not decoration - they are negotiated during the handshake, so a
   replacement listener without them would quietly serve every later transfer at a fraction of the
   speed (measured elsewhere in this file: 78.6 MB/s against 108.7). */
static int pms_make_listener(void) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return -1;
    int opt = 1;
    setsockopt(s, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    int bufsz = 2 << 20;
    setsockopt(s, SOL_SOCKET, SO_RCVBUF, &bufsz, sizeof(bufsz));
    setsockopt(s, SOL_SOCKET, SO_SNDBUF, &bufsz, sizeof(bufsz));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_addr.s_addr = INADDR_ANY;
    a.sin_port = htons(PORT);
    if (bind(s, (struct sockaddr *)&a, sizeof(a)) != 0) { close(s); return -1; }
    if (listen(s, 16) != 0) { close(s); return -1; }
    return s;
}

static void *conn_thread(void *arg) {
    conn_t *c = (conn_t *)arg;
    int fd = c->fd;
    free(c);
    struct timeval to;
    to.tv_sec = 30; to.tv_usec = 0;
    setsockopt(fd, SOL_SOCKET, SO_RCVTIMEO, &to, sizeof(to));
    to.tv_sec = 60;
    setsockopt(fd, SOL_SOCKET, SO_SNDTIMEO, &to, sizeof(to));

    char buf[8192];
    int n = (int)read(fd, buf, sizeof(buf) - 1);
    if (n <= 0) { close(fd); return NULL; }
    buf[n] = 0;

    int is_post = !strncmp(buf, "POST ", 5);
    int is_get  = !strncmp(buf, "GET ", 4);
    int is_head = !strncmp(buf, "HEAD ", 5);
    char path[1024] = "/";
    if (is_get || is_post || is_head) {
        const char *s = buf + ((is_post || is_head) ? 5 : 4);
        const char *e = strchr(s, ' ');
        if (e && (size_t)(e - s) < sizeof(path)) { memcpy(path, s, (size_t)(e - s)); path[e - s] = 0; }
    }

    /* The package stream BGFT pulls. Handled here rather than in handle_get because it is the
       only route that needs the request HEADERS - a Range request - and because the send timeout
       set above must come off first: a multi-gigabyte transfer stalls for longer than a minute
       whenever the console pauses to promote what it already has, and a write that times out
       mid-stream ends the install. */
    if ((is_get || is_head) && !strncmp(path, "/pkgfile/", 9)) {
        serve_pkgfile(fd, path, buf, is_head);
        close(fd);
        return NULL;
    }
    /* GATED IN THE CONDITION, exactly as the PS5 build gates its own: this branch runs before the
       generic path below, so a write that is not allowed must fall THROUGH to the 403 rather than
       be served here. */
    if (is_post && !strncmp(path, "/api/fs/write", 13) &&
        request_origin_ok(buf) && sec_fetch_ok(buf)) {
        char *he = strstr(buf, "\r\n\r\n");
        int hl = he ? (int)(he - buf) + 4 : n;
        fs_recv_write(fd, path, buf, hl, he ? n - hl : 0);
        close(fd);
        return NULL;
    }
    if (!request_origin_ok(buf)) {
        /* A page with a public hostname is driving us. Nothing here is for it. */
        send_status(fd, "403 Forbidden", "text/plain",
                    "this server only answers pages served from a private address");
        close(fd);
        return NULL;
    }
    if (route_changes_state(path, is_post) && !sec_fetch_ok(buf)) {
        /* A browser loaded a state-changing route as an image, a script or a navigation. */
        send_status(fd, "403 Forbidden", "text/plain",
                    "this route changes something and cannot be loaded that way");
        close(fd);
        return NULL;
    }
    if (is_get || is_head) {
        handle_get(fd, path, buf);
    } else if (is_post) {
        /* THE BODY IS BOUNDED BY THE ROOM ACTUALLY LEFT IN buf, NOT BY sizeof(buf).
         *
         * This read `blen < (int)sizeof(buf) - 1` and then filled from `body`, which points INSIDE
         * buf just past the headers. The test never accounted for how far in that was, so a request
         * with 4 KB of headers and Content-Length: 8000 wrote about 4 KB past the end of an 8 KB
         * stack buffer - on a 256 KB thread stack, built with no stack protector, as root, inside a
         * SHARED system daemon, reachable unauthenticated from any machine on the LAN and from any
         * page open in the console's own browser. It was never reached by anything this project
         * ships, which is why it survived; that is luck, not a defence.
         *
         * A body that does not fit gets its own heap buffer instead of being refused: /api/install
         * and friends take JSON that is normally a few hundred bytes, but a long library path or a
         * name with wide characters can push a legitimate request past whatever is left, and a 413
         * for a valid request would be a new bug in place of an old one. The cap is generous and
         * finite so a hostile Content-Length cannot ask for a gigabyte. */
        char *hdr_end = strstr(buf, "\r\n\r\n");
        int blen = 0;
        const char *cl_h = strcasestr_local(buf, "content-length:");
        if (cl_h) blen = atoi(cl_h + 15);
        if (blen < 0) blen = 0;
        char *body = hdr_end ? hdr_end + 4 : NULL;
        char *heap = NULL;
        if (body) {
            int have = n - (int)(body - buf);
            int room = (int)sizeof(buf) - 1 - (int)(body - buf);
            if (room < 0) room = 0;
            if (blen > POST_BODY_MAX) {
                send_status(fd, "413 Payload Too Large", "application/json",
                            "{\"ok\":false,\"error\":\"that request is too large for this console\"}");
                close(fd);
                return NULL;
            }
            if (blen > room) {
                /* Move what has already arrived onto the heap and finish reading there. */
                heap = (char *)malloc((size_t)blen + 1);
                if (!heap) {
                    send_status(fd, "503 Service Unavailable", "application/json",
                                "{\"ok\":false,\"error\":\"the console is out of memory right now\"}");
                    close(fd);
                    return NULL;
                }
                if (have > 0) memcpy(heap, body, (size_t)have);
                body = heap;
            }
            while (have < blen) {
                int r = (int)read(fd, body + have, (size_t)(blen - have));
                if (r <= 0) break;
                have += r;
            }
            if (have < 0) have = 0;
            body[have] = 0;
        }
        handle_post(fd, path, body ? body : "");
        free(heap);
    } else {
        send_status(fd, "405 Method Not Allowed", "text/plain", "GET or POST only");
    }
    close(fd);
    return NULL;
}

/* The work that must not delay the socket. Each step names itself so a stall has an address:
   the last line in klog is the step that hung. */
static void *startup_extras(void *arg) {
    (void)arg;
    /* The install engine first: it is the one thing here anybody waits on, and health reports
       engine_ready from it. If it hangs, everything below simply never runs - and the shop is
       still up, which is the entire point of this thread. */
    ilog("startup: bringing up the install engine");
    bgft_bootstrap();
    ilog("startup: install engine ready");
    /* THE CHEAT LIBRARY HAS TO HAVE SOMEWHERE TO LIVE. cheat_pick_file() reads four of these and
       /api/cheat/paths tells the owner to drop files into a fifth over FTP; none of them existed
       until a PC happened to create them, so a console with no PC had nowhere to put a cheat.
       mkdir on something that is already there fails harmlessly, which is why this is not
       conditional. */
    /* THE AGENT'S MAILBOX, created HERE and not by the agent. The in-game module deliberately
       imports as little as possible - an import a game's libraries cannot resolve fails the module
       load, which is how it kept breaking games - so it has no mkdir. It only ever looks for a
       directory it can write in. This payload runs as root and already owns this tree, so making
       the folder is our job. */
    mkdir(AGENT_DIR, 0777);
    /* AND THE FALLBACK THE AGENT IS TOLD TO TRY. AGENT_DIRS lists /data/GoldHEN/pms-agent second,
       and the agent's choose_dir only probes a directory by trying to write in it - it cannot create
       a missing parent, because it has no mkdir at all. That directory does not exist on the console
       (checked: /data/GoldHEN holds plugins, config.ini, UPDATE, cheats, stats, git, tmp, patches and
       plugins.ini, and nothing else), so the advertised second chance was never a chance. Created
       here, from the root payload, which is the only side that can. */
    mkdir("/data/GoldHEN", 0777);
    mkdir("/data/GoldHEN/pms-agent", 0777);
    mkdir(CHEAT_ROOT, 0777);
    mkdir(CHEAT_JSON_DIR, 0777);
    mkdir(CHEAT_SHN_DIR, 0777);
    mkdir(CHEAT_MC4_DIR, 0777);
    mkdir(CHEAT_PATCH_DIR, 0777);
    mkdir(CHEAT_INBOX_DIR, 0777);
    int filed = cheat_intake_all();
    ilog("startup: cheat library ready, filed %d dropped file(s)", filed);
    if (filed > 0)
        notifyf("Filed %d cheat file%s\nThey are ready in the game panel",
                filed, filed == 1 ? "" : "s");

    /* THE PAYLOAD NEVER ARMS THE IN-GAME PLUGIN. It removes it.
     *
     * Every build of that plugin has broken games on this console - first a freeze on "Please
     * wait...", and on the last attempt badly enough that the console had to be rebooted and
     * re-jailbroken. Both the .prx and its line in plugins.ini SURVIVE a reboot and a re-jailbreak,
     * so a console left armed breaks the next game launch with nothing of ours even running, and
     * there is nothing the owner can press to undo it. Cleaning up has to be automatic and it has
     * to happen on every boot.
     *
     * Deploying is still possible on explicit request through /api/engine/agent, for controlled
     * testing only. Nothing automatic writes it, and nothing automatic lists it. */
    /* THE HELPER SWITCHES ITSELF ON, for exactly the installed titles the library has something for.
     *
     * This used to purge unconditionally, and that was right while the module crashed games: a console
     * left armed re-broke the next launch with nothing of ours running. The cause of that was libc
     * stdio, it is fixed, and the engine is verified working in a real game - so what was left was
     * pure friction, because GoldHEN reads plugins.ini only when a game STARTS. The owner had to press
     * a button and then close and reopen the game. Deciding here removes both steps.
     *
     * THE PURGE IS STILL THE RECOVERY PATH, and it is one file away: with autoarm off, this behaves
     * exactly as it always did. So if an agent build ever misbehaves again, the owner opens the app,
     * turns autoarm off, and every game is clean on the next boot - no re-jailbreak needed. */
    if (autoarm_enabled()) {
        int armed = agent_autoarm_reconcile();
        if (armed >= 0)
            ilog("startup: in-game helper armed for %d installed title(s) with cheats", armed);
        else
            ilog("startup: in-game helper could not be armed - GoldHEN's plugin list was not writable");
    } else {
        int rc = agent_purge();
        ilog("startup: autoarm is off - purge removed %d item(s)", rc);
    }
    ilog("startup: running-title watcher starting");
    rt_thread(NULL);                 /* never returns - this thread becomes the watcher */
    return NULL;
}

int main(void) {
    klog_puts("[PMS] PKG MUTANT SHOP for PS4 starting");
    /* A WRITE TO A CLOSED SOCKET MUST NOT KILL US, and on this console that is not a theoretical
       worry: the transfer service opens the package stream, gives up part-way (a task it decides
       to abandon, a retry, a stop) and closes - and the next write raises SIGPIPE, whose default
       action is to terminate. We are injected into a SHARED system process, so "terminate" means
       the shop vanishes mid-install and takes whatever else lives in that process with it.
       Measured: klog logged "A user thread receives a fatal signal" the first time a package was
       served to the console's own downloader. Ignore it and let write() return EPIPE, which every
       loop here already checks. */
    signal(SIGPIPE, SIG_IGN);
    mkdir("/data", 0777);
    mkdir(SHOP_DATA_DIR, 0777);
    ilog("==== BOOT: PKG MUTANT SHOP PS4 %s (%s %s) ====", SHOP_VERSION, __DATE__, __TIME__);
    /* THE LIBRARY'S COVERS, MADE FROM THE GAMES' OWN FILES, before anybody asks for one. On a
       thread, because one pass decodes every PNG in the library and the connection that asked
       for the first cover must not pay for all of them. */
    thumb_fill_start();

    extract_web();
    /* The payloads this ELF carries, written to PB_DIR the same way the UI is written to WEB_ROOT.
       NOTHING IS STARTED HERE. The only loader on this console is GoldHEN's, and the only way to
       reach it is to POST an ELF at :9090 - which is also the only way to learn whether it is
       listening, because a bare connect stops it. Starting payloads unasked over a lane that
       cannot be probed first is not a thing to do on somebody's console every boot. */
    pb_deploy_all();
    ilog("startup: web extracted");
    /* bgft_bootstrap() USED TO BE HERE AND IT IS WHY THE SHOP VANISHED. klog, measured:

           [PMS] startup: web extracted
           <nothing, ever again>

       It loads four system modules and initialises the download service, and inside a
       ScePartyDaemon that has had several payloads injected into it during one session that call
       stops returning. :8710 then never opens, so there is no shop, no health, no UI and no way
       to see what happened - from outside it is indistinguishable from a payload that never ran.

       The install engine is not needed to LISTEN. It is needed to install, and every install
       route already refuses when g_bgft_ready is false. So it moves to startup_extras() with the
       rest of the optional work, after the socket is up. A console whose install engine is wedged
       now still serves its library, its settings and its cheats, and says engine_ready:false -
       which is a diagnosis instead of a silence. */

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) {
        notify("PKG MUTANT SHOP could not start\nThe console would not give it a network socket");
        return 1;
    }
    g_srv = srv;
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    {
        /* ON THE LISTENING SOCKET, where it still counts: accepted sockets inherit these, and the
           receive window is negotiated during the handshake - setting them after accept() is too
           late to widen it for that connection. Measured on the PS5, which had the identical
           omission: with the 1 MB bulk buffer alone a 128 MB write ran at 78.6 MB/s, and with these
           as well at 108.7 - the link. A failure here is ignored on purpose; it is a performance
           hint, and a console that will not take it must still serve. */
        int bufsz = 2 << 20;
        setsockopt(srv, SOL_SOCKET, SO_RCVBUF, &bufsz, sizeof(bufsz));
        setsockopt(srv, SOL_SOCKET, SO_SNDBUF, &bufsz, sizeof(bufsz));
    }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(PORT);

    /* If an older build of ourselves holds the port, ask it to stand down and take over - the
       normal way this payload is updated. */
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        int taken = 0;
        /* 75 attempts at 400 ms is thirty seconds. Twelve was five, and five is not enough for an
           incumbent that is finishing a transfer before it stands down - the owner then got
           "already running" from a console with no shop on it. This path only runs when somebody
           has deliberately loaded the payload, so waiting is free. */
        for (int attempt = 0; attempt < 75 && !taken; attempt++) {
            if (attempt == 12)
                ilog("serve: :%d is still held - waiting for the incumbent to let go", (int)PORT);
            if (attempt == 0) {
                int q = socket(AF_INET, SOCK_STREAM, 0);
                struct sockaddr_in la;
                memset(&la, 0, sizeof(la));
                la.sin_family = AF_INET;
                la.sin_addr.s_addr = inet_addr("127.0.0.1");
                la.sin_port = htons(PORT);
                struct timeval t2; t2.tv_sec = 3; t2.tv_usec = 0;
                setsockopt(q, SOL_SOCKET, SO_RCVTIMEO, &t2, sizeof(t2));
                if (q >= 0 && connect(q, (struct sockaddr *)&la, sizeof(la)) == 0) {
                    const char *rq = "GET /api/quit HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
                    write_all(q, rq, strlen(rq));
                    char tmp[64];
                    ssize_t rr = read(q, tmp, sizeof(tmp));
                    (void)rr;
                }
                if (q >= 0) close(q);
            }
            /* STILL THERE AFTER ASKING NICELY? TAKE IT. Four attempts is about 1.6 seconds of
               polite waiting, which is long enough for a healthy instance to finish answering
               /api/quit and close its socket. Past that, the incumbent is not answering - and an
               incumbent that cannot answer is exactly the one the owner cannot get rid of. */
            usleep(400000);
            close(srv);
            srv = socket(AF_INET, SOCK_STREAM, 0);
            if (srv < 0) break;
            g_srv = srv;
            setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
            if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) == 0) taken = 1;
        }
        if (!taken) {
            /* SAY WHAT IS ACTUALLY WRONG. "Already running, no need to load it again" was the
               message the owner got from a console that was serving nothing at all, every time
               they tried to fix it. If the port could not be taken even after reclaiming, the
               honest statement is that something else is holding it. */
            /* SAY WHAT IS ACTUALLY WRONG, AND WHAT FIXES IT. "Already running, no need to load
               it again" was the message the owner got from a console that was serving nothing at
               all, every single time they tried to fix it. An earlier instance whose accept loop
               has died holds :8710 in its own process, where nothing outside it can reach the
               socket - measured - so a restart is the honest answer, and this build's accept loop
               is what stops it happening again. */
            notifyf("PKG MUTANT SHOP could not take port %d\n"
                    "An earlier copy is stuck on it - restart the console, then open the app again",
                    (int)PORT);
            ilog("serve: :%d held by an unresponsive earlier instance for 30s - giving up",
                 (int)PORT);
            return 0;
        }
    }
    listen(srv, 16);
    ilog("listening on :%d as %s", PORT, lan_ip_str());

    /* EVERYTHING OPTIONAL HAPPENS FROM HERE, ON ITS OWN THREAD, AFTER THE PORT IS OPEN.
       Laying the in-game agent on disk used to run BEFORE this line and stalled there once: the
       payload logged "web: 5 file(s) written" and was never heard from again, so :8710 never
       opened and the console had no shop at all. Nothing that merely helps cheats may be able to
       do that to a shop that installs games. It only has to finish before the next GAME launches,
       which is many seconds away and not something the accept loop is waiting on. */
    {
        pthread_t bg;
        if (pthread_create(&bg, NULL, startup_extras, NULL) == 0) pthread_detach(bg);
    }
    notifyf("PKG MUTANT SHOP v%s is ready\nOpen %s:%d in any browser", SHOP_VERSION, lan_ip_str(), PORT);

    /* THE DASHBOARD APP, after the socket is listening and not before: the install lane serves the
       package to the console from this very process, so there has to be something to serve it. On
       its own thread - a console that is mid-install must not hold up the shop opening. */
    tile_start();

    g_boot_ms = now_ms();
    long long accept_fails = 0;
    while (!g_quit) {
        /* NO LISTENER MEANS MAKE ONE. The failure branch below drops a socket that has stopped
           accepting, and this is where its replacement is built - so a rebind that does not work
           first time is simply tried again on the next pass instead of leaving the console with
           no shop at all. */
        if (srv < 0) {
            int fresh = pms_make_listener();
            if (fresh < 0) { usleep(250 * 1000); continue; }
            srv = fresh;
            g_srv = fresh;
            accept_fails = 0;
            ilog("serve: listening again on :%d", (int)PORT);
        }
        int cl = accept(srv, 0, 0);
        if (cl < 0) {
            /* accept() failing while g_quit is set IS the handover - the quit handler closed this
               socket on purpose so a newly loaded build can bind. */
            if (g_quit) break;
            /* ANYTHING ELSE IS TRANSIENT - BUT `continue` ON ITS OWN IS A BUSY SPIN, and this code
               runs inside a SHARED system daemon. A persistent failure (out of descriptors, say)
               would have pinned a console's core at 100% with no way to tell why. Back off, and say
               so once rather than every time. */
            /* SAY WHY, AND THEN FIX IT. The first version of this logged a count and nothing else,
               and the count is what caught a real failure - 22,800 consecutive failures with the
               port still open, so the kernel completed every handshake and this process could not
               take a single one. From the outside that is a browser saying the connection was
               forcibly closed, and from here it was invisible apart from the count. errno names it.

               A LISTENING SOCKET THAT CANNOT ACCEPT IS NOT WORTH KEEPING. After a run of failures
               this closes it and binds a new one, which recovers from a descriptor that has been
               closed underneath us or a listener the host has broken - the shop coming back by
               itself rather than waiting for someone to notice and reload it. If the rebind also
               fails it keeps the old socket and carries on trying, because a shop with a wounded
               listener is still better than one with none. */
            int e = errno;
            /* THE NUMBER AND NOTHING MORE. strerror() named this 163 on the console that failed,
               and ELAST is 96 - there is no such errno, so the name was invented. The only fact
               worth logging is that accept keeps failing. */
            if (++accept_fails == 1 || (accept_fails % 200) == 0)
                ilog("serve: accept failed %lld time(s) on :%d - errno %d",
                     accept_fails, (int)PORT, e);
            if ((accept_fails % 50) == 0) {
                /* CLOSE IT, THEN REBIND - IN THAT ORDER, WHICH IS THE WHOLE FIX. This used to
                   bind the replacement while the old socket was still listening on the same
                   port; SO_REUSEADDR does not allow that, so it returned EADDRINUSE every time
                   and this recovery never ran once. Measured: 119,000 consecutive failures with
                   :8710 open and the shop unreachable.
                   g_srv is cleared BEFORE the close so the quit handler cannot close a
                   descriptor number that has already been handed back. */
                int old = srv;
                srv = -1;
                g_srv = -1;
                close(old);
                ilog("serve: the listening socket was not accepting - dropped it after "
                     "%lld failure(s), rebinding", accept_fails);
                continue;                       /* the top of the loop builds the replacement */
            }
            usleep(20 * 1000);
            continue;
        }
        accept_fails = 0;
        g_conns_served++;
        /* One thread per connection: a multi-GB upload or a slow client must never stop the UI
           from answering, which is the trap the PS5 build hit with a single accept loop. */
        conn_t *c = (conn_t *)malloc(sizeof(conn_t));
        if (!c) { close(cl); continue; }
        c->fd = cl;
        pthread_t t;
        pthread_attr_t at;
        pthread_attr_init(&at);
        pthread_attr_setstacksize(&at, 256 * 1024);
        if (pthread_create(&t, &at, conn_thread, c) != 0) { free(c); close(cl); }
        else pthread_detach(t);
        pthread_attr_destroy(&at);
    }
    if (g_srv >= 0) { close(g_srv); g_srv = -1; }
    ilog("stopped serving :%d", PORT);
    return 0;
}

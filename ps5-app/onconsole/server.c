/*
 * PKG MUTANT SHOP - on-console HTTP server  (ps5-payload-dev SDK)
 * ============================================================================
 * Makes the shop INDEPENDENT of the PC: serves the web UI + a local API straight
 * from the PS5, so the dashboard tile works even with the companion PC off.
 *
 *   GET /                -> WEB_ROOT/index.html
 *   GET /<file>          -> WEB_ROOT/<file>            (assets, etc.)
 *   GET /api/health      -> {"ok":true,"on_console":true,...}
 *   GET /api/library     -> installed games (scan /mnt/ext1/homebrew/*.ffpfsc + /user/appmeta/CUSA*)
 *   GET /api/installed   -> {"installed":[...title ids...]}
 *   GET /api/storage     -> {"reachable":true,"drives":[...]}   (from the same scan)
 *   GET /api/icon/<TID>  -> that title's icon0.png              (real box art, on-console)
 *   GET /api/*           -> {}                                   (stub: UI stays happy offline)
 *
 * Binds 0.0.0.0:PORT so it's reachable as http://127.0.0.1:PORT (console browser) AND
 * http://<console-ip>:PORT (curl from the PC, for testing without touching the TV).
 *
 * Build (WSL, ps5-payload-dev SDK):  bash build-wsl.sh
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <stdarg.h>
#include <stdbool.h>
#include <stdint.h>
#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <arpa/inet.h>
#include <sys/stat.h>
#include <sys/statvfs.h>
#include <sys/sysctl.h>
#include <signal.h>
#include <errno.h>
#include <pthread.h>
#include <netdb.h>
#include <poll.h>        /* connect_deadline() - see the note there */
#include <sys/socket.h>
#include <sys/time.h>
#include <netinet/in.h>
#include <netinet/tcp.h>     /* TCP_NODELAY on the bulk streams */

#include <ps5/kernel.h>
#include <ps5/mdbg.h>
#include <ctype.h>       /* tolower() - install-host sniff */
#include "jb.h"
#include "web_bundle.h"
#include "payload_bundle.h"
#include "tile_bundle.h" /* ShadowMount + install host, shipped inside this ELF */
#include "cheat_bundle.h"  /* the whole cheat/patch library, shipped inside this ELF */

#ifndef PORT
#define PORT 8710
#endif
#define SHOP_VERSION "3.97.0"
/* WHICH BINARY IS THIS? SHOP_VERSION is hand-edited, so two different builds can carry the
   same number - and on 2026-08-25 two did, which is why nothing could say which one was
   answering on :8710 when the console died. __DATE__/__TIME__ are filled in by the
   compiler, so every build is distinguishable whether or not anyone remembered to bump
   the version. Reported by /api/health as "built". */
/* No parentheses: this is pasted between two string literals, and "a" ("b") "c" is not
   string concatenation - it is a call expression, which is what the compiler said. */
#define SHOP_BUILD __DATE__ " " __TIME__

/* ShadowMountPlus' HTTP/JSON listener. It announces this itself at startup:
     [API] HTTP/JSON ready: http://127.0.0.1:10101/api/v1 (v1)
   It is NOT 9021 - that is elfldr, the ELF loader, which is always running, so every endpoint
   that probed 9021 reported ShadowMount "active" unconditionally. 3.24.x fixed /api/helpers and
   the boot notification but missed /api/sources and /api/health, which are the two the settings
   panel actually reads. One constant now, so there is nothing left to miss. Loopback-only, so
   only the console can answer this question about itself. */
#define SMP_API_PORT 10101
#ifndef WEB_ROOT
#define WEB_ROOT "/data/pkg-mutant-shop/web"
#endif
#define HOMEBREW_DIR "/mnt/ext1/homebrew"
/* EVERY folder ShadowMount watches. attach_backups() used to look only in HOMEBREW_DIR, so a game
   backed up on a USB drive had no backup_path, reported app.db's metadata size instead of its real
   one, and could not be deleted from the app at all. Measured on this console: 32 backups on ext1
   and 18 on usb0.

   This list is also the SAFETY BOUNDARY for deleting a backup - nothing outside it can be removed,
   which is what keeps "delete from the PS5" from ever being able to touch anything else. */
static const char *HOMEBREW_ROOTS[] = {
    /* the homebrew folders */
    "/mnt/ext1/homebrew", "/mnt/ext0/homebrew", "/mnt/ext2/homebrew", "/data/homebrew",
    "/mnt/usb0/homebrew", "/mnt/usb1/homebrew", "/mnt/usb2/homebrew", "/mnt/usb3/homebrew",
    "/mnt/usb4/homebrew", "/mnt/usb5/homebrew", "/mnt/usb6/homebrew", "/mnt/usb7/homebrew",
    /* /data itself is deliberately absent. Our web bundle, cheat library, logs and installer all
       live under it, and a game container has no reason to sit there rather than /data/homebrew. */
    NULL
};

/* THE DRIVE ROOTS - a SECOND tier with fewer rights. ShadowMount scans the root of a drive as well
   as its homebrew folder, so a container left at /mnt/usb0/ is mounted just the same and has to be
   reachable here, or deleting the game does nothing and the title returns on the next scan.

   They are NOT folded into HOMEBREW_ROOTS, because a drive root is not a folder that exists to hold
   games. This console's /mnt/usb0 also holds PS5 Xplorer v1.05.pkg, InternetBrowser-PS5M.pkg,
   shadowmountplus.elf, an itemzflow folder and Sony's own PS5 backup folder. Inside a watch folder
   "a directory containing sce_sys is a game dump" is a good rule; at the root of a drive the user
   keeps their own things on, it is a bad one. So at this tier we accept CONTAINERS ONLY, and only
   regular files - a directory here is never a deletion candidate, whatever it contains. */
static const char *DRIVE_ROOTS[] = {
    "/mnt/ext0", "/mnt/ext1", "/mnt/ext2",
    "/mnt/usb0", "/mnt/usb1", "/mnt/usb2", "/mnt/usb3",
    "/mnt/usb4", "/mnt/usb5", "/mnt/usb6", "/mnt/usb7",
    NULL
};
/* Payload Manager. Declared here because both the bootstrap that starts helpers and the
   rest-mode shutdown that stops them talk to it, and they sit far apart in this file. */
#define PLDMGR_PORT 8084
#define APPMETA_DIR  "/user/appmeta"

/* On-screen toast.
 *
 * The PS5 shell renders notifications sent through sceKernelSendNotificationRequest — a flat
 * struct of 45 reserved bytes followed by the message. The JSON-payload sceNotificationSend
 * route we used before was accepted (returned 0) but never actually drew anything, which is why
 * toasts silently went missing. Sent on a detached thread because the call can stall when
 * fired repeatedly. */
/* The full request the shell accepts. The trailing icon fields are what let a toast carry
   OUR artwork instead of the generic system glyph; extract_web() has already written the
   icon to WEB_ROOT by the time anything notifies. */
/* THE RECORD THE SHELL ACTUALLY READS. Taken from etaHEN's own header, which this repo vendors at
   research/etahen-2.5B-source/common_utils.h:187-204, and independently confirmed by arithmetic:
   45 + 3075 - the size our working sender has always passed - is exactly 0xC30.

   The previous layout put `message` at 45 by accident (reserved[45] happened to be the right
   padding) but `use_icon_image_uri` at byte 3128, which is OUTSIDE the record. Writing it changed
   nothing the shell reads, and the only real difference between the "icon" call and the "plain"
   call was the LENGTH: 4156 against 3120. That is why toasts vanished whenever branding was turned
   on, and why the recorded conclusion - "this firmware does not render the icon form" - was never
   actually tested. See CHANGELOG 2.6.0 and 3.29.0, both of which assert it. */
typedef struct {
    int32_t type;                  /* 0x00 */
    int32_t req_id;                /* 0x04 */
    int32_t priority;              /* 0x08 */
    int32_t msg_id;                /* 0x0C */
    int32_t target_id;             /* 0x10 */
    int32_t user_id;               /* 0x14 */
    int32_t unk1;                  /* 0x18 */
    int32_t unk2;                  /* 0x1C */
    int32_t app_id;                /* 0x20 */
    int32_t error_num;             /* 0x24 */
    int32_t unk3;                  /* 0x28 */
    char    use_icon_image_uri;    /* 0x2C */
    char    message[1024];         /* 0x2D */
    char    uri[1024];             /* 0x42D */
    char    unkstr[1024];          /* 0x82D */
} notify_request_t;                /* 0xC30 */
_Static_assert(sizeof(notify_request_t) == 0xC30,
               "the shell only draws a 0xC30 record - a padding change here silences every toast");
int sceKernelSendNotificationRequest(int, notify_request_t *, size_t, int);

#define NOTIFY_ICON WEB_ROOT "/assets/icon0.png"
/* The system's own notification glyph, which is what etaHEN's sender uses. Kept so the icon
   experiment can tell "our PNG path is unsupported" apart from "the icon form does not draw". */
#define NOTIFY_ICON_SYSTEM "cxml://psnotification/tex_icon_system"
/* THE SHELL'S VIEW OF OUR ARTWORK. Our process sees /data; the shell sees the same files under
   /user/data - which is precisely why rewrite_for_install() exists (server.c:1818). An icon URI
   beginning /data/ is a path the shell cannot open, so it silently draws no icon. */
#define NOTIFY_ICON_SHELL  "/user/data/pkg-mutant-shop/web/assets/icon0.png"
#define NOTIFY_ICON_FILEURI "file://" NOTIFY_ICON_SHELL
static volatile int g_notify_rc = -12345;      /* what the kernel last returned */
static volatile int g_notify_rc_icon = -12345;

static void *notify_thread(void *arg) {
    notify_request_t *req = (notify_request_t *)arg;
    /* ONE length, for every form. There used to be two - sizeof(*req) for the icon form and
       NOTIFY_BASE_SIZE for the plain one - and since the old struct was 4156 bytes against a
       3120-byte record, choosing the icon form meant sending a length the shell would not draw.
       With the struct corrected, sizeof(*req) IS 0xC30 and there is nothing left to choose. */
    int rc = sceKernelSendNotificationRequest(0, req, sizeof(*req), 0);
    if (req->use_icon_image_uri) g_notify_rc_icon = rc;
    g_notify_rc = rc;
    free(req);
    return NULL;
}

/* THE ICON EXPERIMENT, AND ITS ANSWER (2026-08-26, judged on the television).
 *
 *   variant 1  /data/pkg-mutant-shop/web/assets/icon0.png      text drew, NO icon
 *   variant 2  cxml://psnotification/tex_icon_system            text drew, NO icon
 *   variant 3  /user/data/pkg-mutant-shop/web/assets/icon0.png  text drew, NO icon
 *   variant 4  file:///user/data/.../icon0.png                  text drew, NO icon
 *
 * Variant 2 is byte-for-byte etaHEN's own notify() (commands.cpp:320-343), so etaHEN's toasts
 * carry the standard system glyph too - which looks exactly like a plain one. Variant 3 used the
 * SHELL's view of our file (rewrite_for_install maps /data -> /user/data because ShellCore cannot
 * see ours), and the file is provably there. None of them drew artwork.
 *
 * CONCLUSION: the URI is not the missing piece. The notifications that DO carry artwork - Sony's
 * own "Downloading / Ready to play" - come from BGFT, which raises them itself and resolves the
 * icon from the title's registered metadata in /user/appmeta/<TID>. It is told WHICH TITLE, not
 * where a PNG lives. Carrying artwork here would need the right `type` and an app/content id in
 * the struct's leading int32s, and those values are not known. Do not guess them: a wrong type
 * that makes the shell dereference an id is not a cheap experiment.
 *
 * This costs us nothing in practice. Every install already produces Sony's own artwork
 * notifications, because our installs go through InstallByPackage into BGFT. Our toasts exist for
 * the things the system does not know about - a cheat applied, a backup mounted, rest mode safe -
 * and text is the right medium for those.
 *
 * The variants stay reachable behind GET /api/notify?icon=N so this can be retried deliberately,
 * on the television, without ever changing what the app sends by default. */
static int notify_icon_probe(const char *msg, int variant) {
    notify_request_t *req = (notify_request_t *)calloc(1, sizeof(*req));
    if (!req) return -1;
    snprintf(req->message, sizeof(req->message), "%s", msg ? msg : "");
    req->use_icon_image_uri = 1;
    req->type = 0;
    req->unk3 = 0;
    req->target_id = -1;              /* what etaHEN sets; harmless for the others */
    if (variant == 2)      snprintf(req->uri, sizeof(req->uri), "%s", NOTIFY_ICON_SYSTEM);
    else if (variant == 3) snprintf(req->uri, sizeof(req->uri), "%s", NOTIFY_ICON_SHELL);
    else if (variant == 4) snprintf(req->uri, sizeof(req->uri), "%s", NOTIFY_ICON_FILEURI);
    else                   snprintf(req->uri, sizeof(req->uri), "%s", NOTIFY_ICON);
    int rc = sceKernelSendNotificationRequest(0, req, sizeof(*req), 0);
    g_notify_rc_icon = rc;
    free(req);
    return rc;
}

/* Sent on a detached thread: the call can stall when fired repeatedly. */
static void notify_icon(const char *msg, const char *icon) {
    notify_request_t *req = calloc(1, sizeof(*req));
    if (!req) return;
    snprintf(req->message, sizeof(req->message), "%s", msg ? msg : "");
    if (icon && icon[0]) {
        req->use_icon_image_uri = 1;
        snprintf(req->uri, sizeof(req->uri), "%s", icon);
    }
    pthread_t t;
    pthread_attr_t a;
    pthread_attr_init(&a);
    pthread_attr_setdetachstate(&a, PTHREAD_CREATE_DETACHED);
    /* PS5 default worker stacks are tiny and notify_thread carries a ~3 KB request struct
       plus a syscall frame; every other real thread in this file sets a size explicitly. */
    pthread_attr_setstacksize(&a, 128 * 1024);
    if (pthread_create(&t, &a, notify_thread, req) != 0) free(req);
    pthread_attr_destroy(&a);
}

/* THE PLAIN FORM. Read notify_thread() above before changing this line.
 *
 * The shell accepts the icon form and returns 0 and then draws NOTHING on this firmware. So a
 * branded toast is an invisible toast, and rc == 0 proves only that the call was accepted.
 *
 * This was already discovered, fixed, and documented once. In 3.31.0 it was undone - notify() was
 * pointed at notify_icon() on the strength of "it returns 0 either way" - and every message the
 * app emitted became invisible: startup, installs, cheats, moves, rest mode, all of it. The user
 * reported seeing nothing from us at all while the PS5's own download toasts kept working.
 *
 * If you want to try branding again: do it behind GET /api/notify?icon=1, look at the television,
 * and only then consider changing this. Do not infer rendering from a return code. */
static void notify(const char *msg) { notify_icon(msg, NULL); }

/* HOUSE STYLE FOR EVERY TOAST IN THIS FILE - keep new ones to it:
     line 1  what happened, naming its subject   ("Riptide GP2 is installing")
     line 2  what that means, or what to do next ("Watch the progress on your home screen")
   The icon says who is speaking, so the words "PKG MUTANT SHOP" appear only when the shop itself
   is the subject - starting, stopping, waking. Never put a raw error code, a file path, a JSON
   body or an engineer-facing `detail` string on a television; those belong in the API reply and
   in /api/engine/log, both of which keep them. */

/* Same toast, but sent ON THIS THREAD. Required whenever the process is about to
   exit: notify() hands off to a detached thread, and a returning main() kills that
   thread before it can deliver — which is why loading the payload a second time
   went silent instead of saying it was already running. */
static void notify_sync(const char *msg) {
    notify_request_t *req = (notify_request_t *)calloc(1, sizeof(*req));
    if (!req) return;
    snprintf(req->message, sizeof(req->message), "%s", msg ? msg : "");
    /* Plain, and the one true length. This path runs when the process is about to exit, so an
       invisible toast here is a message the user never gets at all. */
    sceKernelSendNotificationRequest(0, req, sizeof(*req), 0);
    free(req);
}

/* printf-style toast, so callers can say exactly what happened without building buffers.
   The format attribute is NOT decoration: a mismatched argument list here made vsnprintf
   dereference an int and killed the whole process. Let the compiler catch it instead. */
static void notifyf(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void notifyf(const char *fmt, ...) {
    char buf[600];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(buf, sizeof(buf), fmt, ap);
    va_end(ap);
    notify_icon(buf, NULL);          /* plain form - see notify() */
}

/* The one place an install error code becomes words. The TV, the app and the log all read from
   this table, so a code can never mean one thing in a toast and another in the queue.

   Observed on this project, on hardware:
     0x80B2116F  SCE_PLAYGO_ERROR_CORE_INVALID_SLOT - refused BEFORE any download task exists.
                 In our lane this only happens if the installer ran in-process instead of spawned.
     0x80B22404  the installer's own 404 - it fetched the URL and got nothing back.
     0x80F00003  SCE_NP_DRM_CONTENT_ERROR_UNSUPPORTED - not a PKG at all (a mounted backup, say).
     0x80A4xxxx  the console's app database, NOT the package. Suspect console health first.
     0x80B2xxxx  anything else from the installer itself.
   Unknown codes fall through to a sentence that is still true, so this never has to be exhaustive. */
static const char *install_error_text(unsigned rc) {
    switch (rc) {
    case 0x80B2116Fu:
        return "The console turned the request down before it started - reload the shop from "
               "Payload Manager and try again";
    case 0x80B22404u:
        return "The console could not fetch the file - check the PC sharing it is still awake";
    case 0x80B21104u:
        /* Seen when the console refuses an install at submission - no bgft row is created at all.
           Measured on this console with 20.1 GB free and an 85.29 GB package whose own integrity
           check passed. This used to fall into the generic 0x80B2xxxx bucket below, which tells
           the user to download the package again: the one action that cannot possibly help. */
        return "The console would not start this install - it is usually not enough free space on "
               "the drive it installs to";
    case 0x80F00003u:
        return "The console does not recognise this file as a package - it may be a backup, "
               "not a PKG";
    default:
        break;
    }
    if ((rc & 0xFFFF0000u) == 0x80A40000u)
        return "The console's own game database refused it - rebuild the database from Safe Mode, "
               "then try again";
    if ((rc & 0xFFFF0000u) == 0x80B20000u)
        return "The console's installer refused it - the package may be incomplete, so download "
               "it again";
    if (rc == 0u)
        return "";
    return "The console refused it - nothing was installed";
}

/* Said three times, in three files, with three different second lines. Once, here.
   The section on a game's panel is labelled "Mods & Patches" (gp_sec_mods_patches in
   web/index.html); this used to send the user looking for a section called "Cheats". */
static void notify_cheats_filed(int filed) {
    if (filed <= 0) return;
    notifyf("Added %d cheat file%s\nOpen any game in the shop and look under Mods & Patches",
            filed, filed == 1 ? "" : "s");
}

/* The patch equivalent of cheat_result_toast. Same contract: `detail` is engineer-facing and
   stays in the API reply and the log; the television gets a sentence.

   A patch has no documented "off" bytes, so a partial apply cannot be silently undone the way a
   cheat can - which is precisely why the partial case has to be said out loud rather than rounded
   up to "applied". */
static void patch_result_toast(const char *what, int is_revert, int rc,
                               const char *detail, const char *tid) {
    int written = 0;
    const char *w = detail ? strstr(detail, "written=") : NULL;
    if (!w && detail) w = strstr(detail, "wrote=");
    if (w) written = atoi(strchr(w, '=') + 1);

    if (is_revert) {
        if (rc > 0)
            notifyf("%s removed\n%s%sthe game's original code is back", what,
                    (tid && tid[0]) ? tid : "", (tid && tid[0]) ? " - " : "");
        else if (rc == 0)
            notifyf("%s was not applied\nThere was nothing to undo", what);
        else
            notifyf("%s could not be removed\nSome of it is still in the running game - "
                    "close the game to clear it completely", what);
        return;
    }
    if (rc > 0)
        notifyf("%s applied\n%s%s%d change%s written to the running game", what,
                (tid && tid[0]) ? tid : "", (tid && tid[0]) ? " - " : "",
                written, written == 1 ? "" : "s");
    else if (rc == 0)
        notifyf("%s was already applied\nNothing needed changing", what);
    else if (written > 0)
        notifyf("%s only partly applied\n%d change%s written before the rest were refused - "
                "this patch looks built for another version. Remove it to undo them.",
                what, written, written == 1 ? "" : "s");
    else
        notifyf("%s not applied\nIts addresses do not match the game that is running - it is "
                "probably built for another version. Nothing was written.", what);
}

/* The one place a cheat result becomes words. Both cheat routes call this, so they cannot drift
   apart again, and the "how many actually landed" arithmetic lives in exactly one place.

   `detail` carries "written=N ... failed=M" from cheat_apply_mod; rc alone cannot tell you how
   many writes survived a partial failure, which is why the count is parsed back out of it. */
static void cheat_result_toast(const char *what, int want, int rc,
                               const char *detail, const char *tid) {
    int written = 0;
    const char *w = detail ? strstr(detail, "written=") : NULL;
    if (w) written = atoi(w + 8);

    if (rc > 0) {
        notifyf("%s is %s\n%s%s%d change%s written to the running game", what,
                want ? "ON" : "OFF",
                (tid && tid[0]) ? tid : "", (tid && tid[0]) ? " - " : "",
                written, written == 1 ? "" : "s");
    } else if (rc == 0) {
        notifyf("%s was already %s\nNothing needed changing", what, want ? "ON" : "OFF");
    } else if (rc <= -100) {
        /* Some entries were refused by the expect-gate. Whether ANY landed decides which of these
           two very different things we are looking at. */
        if (written > 0)
            notifyf("%s is only partly %s\n%d change%s written before the rest were refused - "
                    "this cheat looks built for another version of the game. Turn it off to "
                    "undo them.",
                    what, want ? "ON" : "OFF", written, written == 1 ? "" : "s");
        else
            notifyf("%s not applied\nThe game's code does not match this cheat file - it is "
                    "probably built for another version. Nothing was written.", what);
    } else if (rc == -4) {
        /* Refused whole: part of this mod could not be read out of its file (an entry larger than
           the engine holds, or malformed hex). Writing the rest would leave a multi-part hook half
           installed - the classic way to hang a game - so nothing was written. */
        notifyf("%s could not be applied\nPart of this cheat is too large or unreadable for the "
                "engine, so none of it was written", what);
    } else {
        /* rc between -1 and -99: the early returns in cheat_apply_mod (unreadable file, no such
           mod, no memory entries). The write loop never ran, so "nothing was written" is certain
           here. `detail` is engineer-facing ("cannot read <path>", "mod 4 not found") and still
           reaches the API response and the logs - it just does not belong on a television. */
        notifyf("%s could not be applied\nIts cheat file is missing or has nothing to write "
                "for this game. Nothing was written - try Rescan cheats in Settings.", what);
    }
}


/* ---------------- the install log ---------------- */
/* SHOP_DATA_DIR is defined further down with the rest of the on-console paths; this block sits
   above it, so the literal is spelled out here rather than reordering the file. */
#define ILOG_PATH      "/data/pkg-mutant-shop/install.log"
#define ILOG_MAX_BYTES (512 * 1024)

static pthread_mutex_t g_ilog_lock = PTHREAD_MUTEX_INITIALIZER;

static void ilog(const char *fmt, ...) __attribute__((format(printf, 1, 2)));
static void ilog(const char *fmt, ...) {
    char line[900], body[760];
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(body, sizeof(body), fmt, ap);
    va_end(ap);

    struct timeval tv;
    gettimeofday(&tv, NULL);
    int n2 = snprintf(line, sizeof(line), "%lld.%03d  %s\n",
                      (long long)tv.tv_sec, (int)(tv.tv_usec / 1000), body);
    if (n2 <= 0) return;

    pthread_mutex_lock(&g_ilog_lock);
    mkdir("/data/pkg-mutant-shop", 0777);      /* may be the very first thing to touch it */
    int fd = open(ILOG_PATH, O_WRONLY | O_CREAT | O_APPEND | O_SYNC, 0666);
    if (fd >= 0) {
        ssize_t w = write(fd, line, (size_t)n2);
        (void)w;
        close(fd);
    }
    pthread_mutex_unlock(&g_ilog_lock);
}

/* Keep it bounded. At startup only: truncating a log while an install is in flight is exactly the
   write we do not want to be making. */
static void ilog_trim_at_boot(void) {
    struct stat st;
    if (stat(ILOG_PATH, &st) != 0 || st.st_size <= ILOG_MAX_BYTES) return;
    int fd = open(ILOG_PATH, O_RDONLY);
    if (fd < 0) return;
    size_t keep = ILOG_MAX_BYTES / 2;
    if (lseek(fd, (off_t)(st.st_size - (off_t)keep), SEEK_SET) < 0) { close(fd); return; }
    char *buf = (char *)malloc(keep + 1);
    if (!buf) { close(fd); return; }
    ssize_t got = read(fd, buf, keep);
    close(fd);
    if (got > 0) {
        int out = open(ILOG_PATH ".tmp", O_WRONLY | O_CREAT | O_TRUNC, 0666);
        if (out >= 0) {
            ssize_t w = write(out, buf, (size_t)got);
            (void)w;
            close(out);
            rename(ILOG_PATH ".tmp", ILOG_PATH);
        }
    }
    free(buf);
}

/* ---------------- small write helpers ---------------- */
static void write_all(int fd, const char *buf, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t k = write(fd, buf + off, len - off);
        if (k <= 0) break;
        off += (size_t)k;
    }
}

/* Same loop, but it says whether the bytes actually landed. write_all() returns void and is called
   from dozens of socket paths where a dropped client is not worth unwinding; on a FILE it is a
   different matter. A full /data made every write fail silently while the download counter went on
   climbing, so `have` reached the expected size, the transfer "succeeded", and a truncated PKG was
   handed to the installer. Returns the bytes written, or -1. */
static ssize_t write_all_checked(int fd, const char *buf, size_t len) {
    size_t off = 0;
    while (off < len) {
        ssize_t k = write(fd, buf + off, len - off);
        if (k < 0) {
            if (errno == EINTR) continue;
            return -1;
        }
        if (k == 0) return -1;              /* no progress: out of space, or the fd went away */
        off += (size_t)k;
    }
    return (ssize_t)off;
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
static void send_json(int fd, const char *body) { send_status(fd, "200 OK", "application/json", body); }

static const char *ctype_for(const char *path) {
    const char *dot = strrchr(path, '.');
    if (!dot) return "application/octet-stream";
    if (!strcmp(dot, ".html")) return "text/html; charset=utf-8";
    if (!strcmp(dot, ".css"))  return "text/css";
    if (!strcmp(dot, ".js"))   return "application/javascript";
    if (!strcmp(dot, ".json")) return "application/json";
    if (!strcmp(dot, ".png"))  return "image/png";
    if (!strcmp(dot, ".jpg") || !strcmp(dot, ".jpeg")) return "image/jpeg";
    if (!strcmp(dot, ".svg"))  return "image/svg+xml";
    if (!strcmp(dot, ".ico"))  return "image/x-icon";
    return "application/octet-stream";
}

/* stream a file from disk with correct headers; returns 0 on success, -1 if missing */
static int icon_exists(const char *tid);   /* defined with the icon server below */
static void best_pc_base(char *out, size_t outsz);  /* best companion base URL, defined below */


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

/* `req` is the raw request text, or NULL from callers that have no request in hand (the log file,
   the spawn result). NULL simply means "never a 304", which is the behaviour every caller had
   before this parameter existed. */
static int send_file_req(int fd, const char *path, const char *req) {
    int f = open(path, O_RDONLY);
    if (f < 0) return -1;
    struct stat st;
    if (fstat(f, &st) != 0 || !S_ISREG(st.st_mode)) { close(f); return -1; }
    char hdr[640];
    /* CACHING. This server sent no Cache-Control, no ETag and no Last-Modified, so a browser had
       no validator and reused nothing - every grid rebuild re-fetched every ~360 KB cover through
       this SINGLE-THREADED accept loop, queued behind the install engine and the cheat engine. An
       icon is immutable for a given title and the UI assets only change when a new ELF is loaded
       (extract_web rewrites them), so a long max-age plus a size+mtime ETag is safe - and it is the
       single biggest win available for the console UI. */
    /* CACHE POLICY BY TYPE. A blanket long max-age was tried and is wrong: it also cached
       index.html, so loading a NEW ELF left the console running the OLD UI for a week - the
       browser never asked again. Artwork is immutable for a title and is the expensive thing, so
       it keeps the long max-age. The app shell must always revalidate; it is small, and the ETag
       means an unchanged shell still costs only a 304. */
    const char *ct = ctype_for(path);
    int is_img = ct && (strstr(ct, "image/") || strstr(ct, "font"));
    const char *cache = is_img ? "public, max-age=604800, immutable"
                               : "no-cache, must-revalidate";
    char etag[64];
    snprintf(etag, sizeof(etag), "\"%llx-%llx\"",
             (unsigned long long)st.st_size, (unsigned long long)st.st_mtime);
    if (etag_matches(req, etag)) {
        /* Same bytes as the copy the browser already holds. No body, and the file is not read. */
        close(f);
        char h3[320];
        int n3 = snprintf(h3, sizeof(h3),
            "HTTP/1.1 304 Not Modified\r\nETag: %s\r\nCache-Control: %s\r\n"
            "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n", etag, cache);
        write_all(fd, h3, (size_t)n3);
        return 0;
    }
    int n = snprintf(hdr, sizeof(hdr),
        "HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %lld\r\n"
        "Cache-Control: %s\r\nETag: %s\r\n"
        "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n",
        ct, (long long)st.st_size, cache, etag);
    write_all(fd, hdr, (size_t)n);
    char buf[65536];
    for (;;) {
        ssize_t r = read(f, buf, sizeof(buf));
        if (r <= 0) break;
        write_all(fd, buf, (size_t)r);
    }
    close(f);
    return 0;
}
static int send_file(int fd, const char *path) { return send_file_req(fd, path, NULL); }

/* ---------------- library scan (no sqlite; filenames + dirs carry everything) ---------------- */
/* Map a contentLocation-ish label. Homebrew backups live on ext storage. */
static void json_escape(const char *in, char *out, size_t outsz) {
    size_t j = 0;
    for (size_t i = 0; in[i] && j < outsz - 2; i++) {
        unsigned char c = (unsigned char)in[i];
        if (c == '"' || c == '\\') { out[j++] = '\\'; out[j++] = (char)c; }
        else if (c == '\n' || c == '\r' || c == '\t') out[j++] = ' ';
        else if (c < 0x20) continue;                /* control bytes have no place in a name */
        else if (c >= 0x80) {
            /* Pass through a VALID UTF-8 sequence so "Bloodborne™" keeps its ™ instead of losing
               it. Only genuinely broken bytes are dropped — which is what this used to do to
               everything non-ASCII, taking real characters out of real titles with the mojibake. */
            int need = (c >= 0xF0) ? 3 : (c >= 0xE0) ? 2 : (c >= 0xC2) ? 1 : -1;
            if (need < 0) continue;                 /* stray continuation or overlong lead */
            int ok = 1;
            for (int k2 = 1; k2 <= need; k2++)
                if (((unsigned char)in[i + k2] & 0xC0) != 0x80) { ok = 0; break; }
            if (!ok) continue;
            if (j + (size_t)need + 1 >= outsz - 2) break;
            out[j++] = (char)c;
            for (int k2 = 1; k2 <= need; k2++) out[j++] = in[i + k2];
            i += (size_t)need;
        }
        else out[j++] = (char)c;
    }
    out[j] = 0;
}

/* find "CUSAxxxxx" or "PPSAxxxxx" inside a string; copies 9 chars to tid (must be >=10). 1 if found. */
static int find_tid(const char *s, char *tid) {
    for (const char *p = s; *p; p++) {
        if ((p[0]=='C'||p[0]=='P') && (p[1]=='U'||p[1]=='P') && p[2]=='S' && p[3]=='A') {
            int digits = 1;
            for (int k = 4; k < 9; k++) if (p[k] < '0' || p[k] > '9') { digits = 0; break; }
            if (digits) { memcpy(tid, p, 9); tid[9] = 0; return 1; }
        }
    }
    return 0;
}

/* Build the /api/library JSON into a heap buffer (caller frees). */
/* Defined further down; needed by the console-title reader below. */
static char *slurp(const char *path, long *out_len);
static int path_ext_is(const char *path, const char *ext);

/* Every container ShadowMount can run in place. One list, checked everywhere a backup is
   recognised — the three places that each carried their own shorter list disagreed, so a stick
   holding an .exfat or .iso backup was listed by one scan and ignored by the next. */
static const char *BACKUP_EXTS[] = { ".ffpfsc", ".ffpfs", ".ffpkg", ".ffpfsx",
                                     ".fpkg", ".exfat", ".iso", ".img", NULL };
/* What makes a DIRECTORY a game dump rather than an ordinary folder. Mirrored from
   GAME_FOLDER_MARKERS in companion/server.py - a folder-shaped backup is recognised by its
   structure, never by its name, and this end has to agree with that end or a dump that the PC will
   happily send becomes one the console cannot find again. */
static const char *GAME_FOLDER_MARKERS[] = {
    "eboot.bin", "sce_sys", "param.json", "param.sfo", "sce_module", NULL
};

static int looks_like_game_folder(const char *dir) {
    for (int i = 0; GAME_FOLDER_MARKERS[i]; i++) {
        char p[700];
        struct stat st;
        snprintf(p, sizeof(p), "%s/%s", dir, GAME_FOLDER_MARKERS[i]);
        if (stat(p, &st) == 0) return 1;
    }
    return 0;
}

static int is_backup_ext(const char *name) {
    for (int i = 0; BACKUP_EXTS[i]; i++) if (path_ext_is(name, BACKUP_EXTS[i])) return 1;
    return 0;
}

/* ---------------------------------------------------------------------------
 * sqmini — read-only, single-table SQLite scan of the console's app.db.
 *
 * app.db is the ONLY place holding real title names, sizes, install locations
 * and installed versions. The PC companion reads it with Python's sqlite3;
 * standalone on the console we have no such luxury, and linking a full SQLite
 * for one sequential scan would be absurd. This does that scan and nothing
 * else: no writes, no journal, no locking, no SQL.
 * ------------------------------------------------------------------------- */
typedef struct {
    const uint8_t *data;
    size_t         size;
    uint32_t       page_size;
    uint32_t       usable;      /* page_size minus the per-page reserved tail */
} sqdb_t;

/* Row callback: vals[i] is NUL-terminated (never NULL — missing becomes ""). */
typedef void (*sq_row_cb)(void *ctx, int ncol, char **vals);

static int sq_open(sqdb_t *db, const uint8_t *data, size_t size) {
    if (!data || size < 512 || memcmp(data, "SQLite format 3", 16)) return -1;
    uint32_t ps = (uint32_t)((data[16] << 8) | data[17]);
    if (ps == 1) ps = 65536;
    if (ps < 512 || (ps & (ps - 1))) return -1;         /* must be a power of two */
    db->data = data; db->size = size; db->page_size = ps;
    db->usable = ps - data[20];
    return 0;
}

static const uint8_t *sq_page(sqdb_t *db, uint32_t pgno) {
    if (pgno == 0) return NULL;
    size_t off = (size_t)(pgno - 1) * db->page_size;
    if (off + db->page_size > db->size) return NULL;
    return db->data + off;
}

/* SQLite varint: up to 9 bytes, big-endian, 7 bits per byte (the 9th gives 8). */
static int sq_varint(const uint8_t *p, const uint8_t *end, uint64_t *out) {
    uint64_t v = 0;
    int i = 0;
    for (; i < 8; i++) {
        if (p + i >= end) return -1;
        v = (v << 7) | (uint64_t)(p[i] & 0x7F);
        if (!(p[i] & 0x80)) { *out = v; return i + 1; }
    }
    if (p + 8 >= end) return -1;
    v = (v << 8) | p[8];
    *out = v;
    return 9;
}

static uint32_t sq_be32(const uint8_t *p) {
    return ((uint32_t)p[0] << 24) | ((uint32_t)p[1] << 16) | ((uint32_t)p[2] << 8) | p[3];
}

/* Assemble a cell payload, following the overflow chain when it does not fit
   on the page. Returns malloc'd bytes of length total. */
static uint8_t *sq_payload(sqdb_t *db, const uint8_t *cell, const uint8_t *page_end,
                           uint64_t total, size_t *out_len) {
    uint32_t U = db->usable;
    uint32_t X = U - 35;                       /* max local payload, table leaf */
    uint32_t local;
    if (total <= X) {
        local = (uint32_t)total;
    } else {
        uint32_t M = ((U - 12) * 32 / 255) - 23;
        uint32_t K = M + (uint32_t)((total - M) % (U - 4));
        local = (K <= X) ? K : M;
    }
    if (cell + local > page_end) return NULL;
    uint8_t *buf = (uint8_t *)malloc((size_t)total + 1);
    if (!buf) return NULL;
    memcpy(buf, cell, local);
    size_t got = local;
    if (got < total) {
        if (cell + local + 4 > page_end) { free(buf); return NULL; }
        uint32_t next = sq_be32(cell + local);
        int guard = 0;
        while (got < total && next && guard++ < 4096) {
            const uint8_t *op = sq_page(db, next);
            if (!op) break;
            size_t chunk = U - 4;
            if (chunk > total - got) chunk = (size_t)(total - got);
            memcpy(buf + got, op + 4, chunk);
            got += chunk;
            next = sq_be32(op);
        }
    }
    if (got != total) { free(buf); return NULL; }
    buf[total] = 0;
    *out_len = (size_t)total;
    return buf;
}

/* Decode one record into ncol NUL-terminated strings (caller frees each). */
static int sq_record(const uint8_t *rec, size_t rec_len, int ncol, char **vals) {
    for (int i = 0; i < ncol; i++) vals[i] = NULL;
    const uint8_t *end = rec + rec_len;
    uint64_t hdr_size = 0;
    int n = sq_varint(rec, end, &hdr_size);
    if (n < 0 || hdr_size > rec_len) return -1;
    const uint8_t *tp = rec + n;                 /* serial types */
    const uint8_t *hdr_end = rec + hdr_size;
    const uint8_t *body = hdr_end;
    for (int col = 0; col < ncol; col++) {
        if (tp >= hdr_end) break;                /* fewer columns than asked: leave "" */
        uint64_t st = 0;
        int k = sq_varint(tp, hdr_end, &st);
        if (k < 0) break;
        tp += k;
        size_t len = 0;
        int is_int = 0;
        int64_t iv = 0;
        switch (st) {
            case 0: len = 0; break;
            case 1: len = 1; is_int = 1; break;
            case 2: len = 2; is_int = 1; break;
            case 3: len = 3; is_int = 1; break;
            case 4: len = 4; is_int = 1; break;
            case 5: len = 6; is_int = 1; break;
            case 6: len = 8; is_int = 1; break;
            case 7: len = 8; break;              /* float — not needed, skipped */
            case 8: is_int = 1; iv = 0; len = 0; break;
            case 9: is_int = 1; iv = 1; len = 0; break;
            default:
                if (st >= 12) len = (size_t)((st - 12 - (st & 1)) / 2);
                break;
        }
        if (body + len > end) break;
        if (is_int) {
            if (len) {
                int64_t v = (body[0] & 0x80) ? -1 : 0;   /* sign-extend */
                for (size_t b = 0; b < len; b++) v = (v << 8) | body[b];
                iv = v;
            }
            char tmp[24];
            snprintf(tmp, sizeof(tmp), "%lld", (long long)iv);
            vals[col] = strdup(tmp);
        } else if (st >= 12) {
            char *s = (char *)malloc(len + 1);
            if (s) { memcpy(s, body, len); s[len] = 0; vals[col] = s; }
        }
        body += len;
    }
    for (int i = 0; i < ncol; i++) if (!vals[i]) vals[i] = strdup("");
    return 0;
}

/* Walk a table b-tree, decoding every leaf row. */
static void sq_walk(sqdb_t *db, uint32_t pgno, int ncol, sq_row_cb cb, void *ctx, int depth) {
    if (depth > 32) return;                       /* corrupt/looping tree guard */
    const uint8_t *pg = sq_page(db, pgno);
    if (!pg) return;
    const uint8_t *hdr = (pgno == 1) ? pg + 100 : pg;   /* page 1 carries the file header */
    uint8_t type = hdr[0];
    uint16_t ncell = (uint16_t)((hdr[3] << 8) | hdr[4]);
    const uint8_t *page_end = pg + db->usable;

    if (type == 0x05) {                            /* interior table page */
        const uint8_t *ptrs = hdr + 12;
        for (uint16_t i = 0; i < ncell; i++) {
            const uint8_t *pp = ptrs + i * 2;
            if (pp + 2 > page_end) break;
            uint32_t off = (uint32_t)((pp[0] << 8) | pp[1]);
            if (off + 4 > db->usable) continue;
            sq_walk(db, sq_be32(pg + off), ncol, cb, ctx, depth + 1);
        }
        sq_walk(db, sq_be32(hdr + 8), ncol, cb, ctx, depth + 1);   /* rightmost child */
        return;
    }
    if (type != 0x0D) return;                      /* not a table leaf */

    const uint8_t *ptrs = hdr + 8;
    for (uint16_t i = 0; i < ncell; i++) {
        const uint8_t *pp = ptrs + i * 2;
        if (pp + 2 > page_end) break;
        uint32_t off = (uint32_t)((pp[0] << 8) | pp[1]);
        if (off >= db->usable) continue;
        const uint8_t *cell = pg + off;
        uint64_t plen = 0, rowid = 0;
        int a = sq_varint(cell, page_end, &plen);
        if (a < 0) continue;
        int b = sq_varint(cell + a, page_end, &rowid);
        if (b < 0) continue;
        size_t got = 0;
        uint8_t *rec = sq_payload(db, cell + a + b, page_end, plen, &got);
        if (!rec) continue;
        char **vals = (char **)calloc((size_t)ncol, sizeof(char *));
        if (vals) {
            if (sq_record(rec, got, ncol, vals) == 0) cb(ctx, ncol, vals);
            for (int c = 0; c < ncol; c++) free(vals[c]);
            free(vals);
        }
        free(rec);
    }
}

/* --- sqlite_master lookup: root page + CREATE TABLE sql for one table ------ */
typedef struct { const char *want; uint32_t root; char *sql; } sq_find_t;

static void sq_master_cb(void *ctx, int ncol, char **v) {
    sq_find_t *f = (sq_find_t *)ctx;
    (void)ncol;
    if (f->root) return;
    if (strcmp(v[0], "table")) return;             /* type */
    if (strcmp(v[1], f->want)) return;             /* name */
    f->root = (uint32_t)strtoul(v[3], NULL, 10);   /* rootpage */
    f->sql  = strdup(v[4] ? v[4] : "");
}

/* Column index by name, parsed out of the CREATE TABLE statement. Doing it this
   way means a firmware that adds or reorders columns cannot silently shift our
   reads onto the wrong field. Returns -1 when absent. */
static int sq_col_index(const char *sql, const char *col) {
    if (!sql) return -1;
    const char *p = strchr(sql, '(');
    if (!p) return -1;
    p++;
    int depth = 0, idx = 0;
    size_t cl = strlen(col);
    while (*p) {
        while (*p == ' ' || *p == '\n' || *p == '\r' || *p == '\t' || *p == ',') p++;
        if (*p == ')' && depth == 0) break;
        const char *name = p;
        size_t n = 0;
        if (*name == '"' || *name == '`' || *name == '[') {     /* quoted identifier */
            char close = (*name == '[') ? ']' : *name;
            name++;
            const char *e = strchr(name, close);
            if (!e) break;
            n = (size_t)(e - name);
            p = e + 1;
        } else {
            while (p[n] && p[n] != ' ' && p[n] != ',' && p[n] != '(' && p[n] != ')') n++;
            p = name + n;
        }
        if (n == cl && !strncmp(name, col, cl)) return idx;
        /* skip to the comma that ends this column definition, at depth 0 */
        while (*p) {
            if (*p == '(') depth++;
            else if (*p == ')') { if (depth == 0) break; depth--; }
            else if (*p == ',' && depth == 0) break;
            p++;
        }
        if (*p == ',') { p++; idx++; continue; }
        break;
    }
    return -1;
}

static const char *json_str_after(const char *p, const char *key, char *out, size_t outsz);
static const char *json_str_after_lim(const char *p, const char *end, const char *key,
                                      char *out, size_t outsz);

/* ---------------- every PC on the network, not just the last one ----------
 * Each companion announces itself to us. This used to be a SINGLE slot, so the
 * second PC to start overwrote the first and the console only ever knew about
 * whichever machine happened to register last — which is why the library looked
 * different depending on which PC you had open.
 *
 * We keep them all, ask each one what it holds, and merge the lot. Nothing here
 * depends on a PC staying awake: an unreachable one is simply skipped.
 * ------------------------------------------------------------------------- */
#define PC_MAX 8
typedef struct {
    char ip[24];
    int  port;
    char name[64];
    long long last_ms;
    int  count;                 /* titles this PC advertises, for the Devices panel */
    long long bytes;            /* how much it is holding, for the storage bar */
    char ver[16];               /* its app version - we must prefer the NEWEST companion */
    /* WHEN THIS PC LAST FAILED TO ANSWER. A companion announces itself every 8 seconds and is
       considered gone after PC_STALE_MS, so there is a ten-minute window in which a PC that was
       switched off a moment ago still looks present and is still dialled. At CONNECT_MS each, on
       the accept loop, that is a hitch in every library request for ten minutes. One failure now
       buys PC_FAIL_BACKOFF_MS of not asking again. */
    long long fail_ms;
} pcpeer_t;
static const char *lan_ip_str(void);   /* defined with the other network helpers below */
static pcpeer_t g_pcs[PC_MAX];
static pthread_mutex_t g_pcs_lock = PTHREAD_MUTEX_INITIALIZER;

/* Connections this process has accepted, and when it started. Published in /api/health so that
   "the port is open but nothing is being served" is a number somebody can read rather than a guess
   - which is exactly the state the owner hit, and the state nothing could describe at the time. */
static volatile long long g_conns_served = 0;
static long long g_started_ms = 0;

static long long now_ms_local(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* Compare dotted versions numerically: "3.20.2" > "3.19.0". A string compare gets that wrong
   because "19" sorts after "2". Missing or blank sorts lowest. */
static int ver_cmp(const char *a, const char *b) {
    for (;;) {
        long x = 0, y = 0;
        if (a && *a) { x = strtol(a, (char **)&a, 10); if (*a == '.') a++; }
        if (b && *b) { y = strtol(b, (char **)&b, 10); if (*b == '.') b++; }
        if (x != y) return x > y ? 1 : -1;
        if ((!a || !*a) && (!b || !*b)) return 0;
    }
}

/* A COMPANION THAT HAS NOT ANNOUNCED ITSELF FOR THIS LONG IS NOT THERE ANY MORE.
   Nothing ever evicted one. A companion started once on a spare port stayed in this list for as
   long as the payload was loaded, and the page walked down to it whenever the real PC was briefly
   away - which is exactly how the owner saw their browser reaching for :8791, a port that had not
   existed for hours. Same value the PS4 port already uses. */
#define PC_STALE_MS (10 * 60 * 1000LL)
/* A PC that just refused a connection is not asked again for this long. */
#define PC_FAIL_BACKOFF_MS (60 * 1000LL)
/* AND A SEPARATE, MUCH SHORTER SILENCE BEFORE WE STOP DIALLING ONE. Companions announce themselves
   every 8 seconds; five missed announcements is not a machine worth opening a socket to. This is
   deliberately NOT PC_STALE_MS: ten minutes is the right answer to "stop offering this PC to the
   page" (a lid closed for a moment should not empty the library) and the wrong answer to "keep
   trying to reach it", which is the question that costs the accept loop its time. */
#define PC_DIAL_STALE_MS (45 * 1000LL)
/* AND A CEILING ON THE WHOLE MERGE, whatever the table holds. PC_MAX is 8: without this, eight
   entries that each cost CONNECT_MS is twelve seconds of a single-threaded server, and the failure
   this guards against is precisely one where several PCs are off at once. */
#define PEER_MERGE_BUDGET_MS 2500LL
/* The largest POST body this console will take. Generous for the JSON every route here expects,
   finite so a hostile Content-Length cannot ask for a gigabyte. Same value the PS4 uses. */
#define POST_BODY_MAX (256 * 1024)

static void pc_register(const char *ip, int port) {
    if (!ip || !ip[0] || port <= 0) return;
    pthread_mutex_lock(&g_pcs_lock);
    int slot = -1, oldest = 0;
    for (int i = 0; i < PC_MAX; i++) {
        /* KEYED ON THE ADDRESS ALONE. Keying on (ip, port) meant a companion that moved to another
           port took a SECOND slot instead of updating its own, and with nothing ageing entries out
           the abandoned one outlived it. One machine is one entry; the newest port it announced is
           the one it is on. */
        if (!strcmp(g_pcs[i].ip, ip)) { slot = i; break; }
        if (!g_pcs[i].ip[0]) { slot = i; break; }
        if (g_pcs[i].last_ms < g_pcs[oldest].last_ms) oldest = i;
    }
    if (slot < 0) slot = oldest;                 /* full: replace the least recently seen */
    snprintf(g_pcs[slot].ip, sizeof(g_pcs[slot].ip), "%s", ip);
    g_pcs[slot].port = port;
    g_pcs[slot].last_ms = now_ms_local();
    /* IT IS ANNOUNCING ITSELF, SO IT IS UP. Without this a PC that woke stayed in the backoff for
       a minute and its games stayed missing from a console that could already hear it. */
    g_pcs[slot].fail_ms = 0;
    pthread_mutex_unlock(&g_pcs_lock);
}

/* Record a companion's app version, learned at registration. Separate from pc_register() so the
   registration path stays exactly as it was for every other caller. */
static void pc_set_version(const char *ip, int port, const char *ver) {
    if (!ip || !ip[0] || !ver || !ver[0]) return;
    pthread_mutex_lock(&g_pcs_lock);
    for (int i = 0; i < PC_MAX; i++)
        if (!strcmp(g_pcs[i].ip, ip) && g_pcs[i].port == port) {
            snprintf(g_pcs[i].ver, sizeof(g_pcs[i].ver), "%s", ver);
            break;
        }
    pthread_mutex_unlock(&g_pcs_lock);
}

/* HOW LONG A HOST THAT IS SWITCHED OFF MAY COST US. A LAN round trip is under a millisecond;
   anything that has not completed the handshake in this long is not coming. */
#define CONNECT_MS      1500
/* Fetching a file is a deliberate act against a host the owner named, so it gets more room. */
#define CONNECT_DL_MS   5000

/* connect() WITH A DEADLINE, which this server did not have and badly needed.
 *
 * SO_RCVTIMEO AND SO_SNDTIMEO DO NOT BOUND connect(). They govern read() and write(). Every
 * outbound helper here set both, six seconds apart, and read as though it were covered. It was not.
 * A connect() to a host that is powered off cannot fail fast: nothing answers the SYN, so the stack
 * runs its whole retry schedule first - and these headers are FreeBSD 11.4, where that is
 * net.inet.tcp.keepinit, 75 seconds.
 *
 * WHAT THAT COST, MEASURED FROM THE OWNER'S REPORT (2026-10-02): with the companion PC switched
 * off, this console's shop stopped answering ANYTHING. /api/library is served inline on the single
 * accept loop and merges each registered PC's library over HTTP; the first PC in the table was the
 * one that was off, so the loop sat inside connect() for 75 seconds at a time. The page itself
 * could not be fetched - the browser showed the bare URL as its title, which is what a page that
 * never loaded looks like - and relaunching the ELF said "already running", because the port was
 * still bound the whole time. A second, perfectly healthy PC on the same network did not help: the
 * loop blocks on the dead entry before it ever reaches the live one. The PS4 was unaffected because
 * it gives every connection its own thread.
 *
 * Non-blocking connect, wait for writability with a deadline, then ask SO_ERROR what actually
 * happened - a socket that is merely writable may still have been refused. The descriptor goes back
 * to blocking before it is handed on, because every caller read()s it with SO_RCVTIMEO set. */
static int connect_deadline(int s, const struct sockaddr *sa, socklen_t slen, int ms) {
    int fl = fcntl(s, F_GETFL, 0);
    if (fl < 0) return -1;
    if (fcntl(s, F_SETFL, fl | O_NONBLOCK) < 0) return -1;
    int rc = connect(s, sa, slen);
    if (rc != 0) {
        if (errno != EINPROGRESS) { fcntl(s, F_SETFL, fl); return -1; }
        struct pollfd p;
        p.fd = s; p.events = POLLOUT; p.revents = 0;
        int pr;
        do { pr = poll(&p, 1, ms); } while (pr < 0 && errno == EINTR);
        if (pr <= 0) { fcntl(s, F_SETFL, fl); return -1; }   /* 0 is the deadline, and that is the point */
        int err = 0;
        socklen_t el = sizeof(err);
        if (getsockopt(s, SOL_SOCKET, SO_ERROR, &err, &el) != 0 || err != 0) {
            fcntl(s, F_SETFL, fl);
            return -1;
        }
    }
    fcntl(s, F_SETFL, fl);
    return 0;
}

/* Plain HTTP GET into memory from a dotted IPv4 address. Companions register by
   address, so no name resolution is needed. Returns malloc'd body or NULL. */
static char *http_get_ip_to(const char *ip, int port, const char *path, long *out_len, int rcv_s) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return NULL;
    struct timeval tv = { rcv_s, 0 };
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)port);
    if (inet_pton(AF_INET, ip, &a.sin_addr) != 1) { close(s); return NULL; }
    if (connect_deadline(s, (struct sockaddr *)&a, sizeof(a), CONNECT_MS) != 0) { close(s); return NULL; }

    char req[512];
    int n = snprintf(req, sizeof(req),
                     "GET %s HTTP/1.1\r\nHost: %s:%d\r\nConnection: close\r\n"
                     "User-Agent: pkg-mutant-shop\r\n\r\n", path, ip, port);
    write_all(s, req, (size_t)n);

    size_t cap = 1 << 16, len = 0;
    char *buf = (char *)malloc(cap);
    if (!buf) { close(s); return NULL; }
    for (;;) {
        if (len + 8192 > cap) {
            if (cap > (8u << 20)) break;          /* a peer library should never be this big */
            cap *= 2;
            char *nb = (char *)realloc(buf, cap);
            if (!nb) break;
            buf = nb;
        }
        ssize_t r = read(s, buf + len, cap - len - 1);
        if (r <= 0) break;
        len += (size_t)r;
    }
    close(s);
    buf[len] = 0;
    char *body = strstr(buf, "\r\n\r\n");
    if (!body) { free(buf); return NULL; }
    body += 4;
    size_t blen = len - (size_t)(body - buf);
    memmove(buf, body, blen + 1);
    if (out_len) *out_len = (long)blen;
    return buf;
}

/* The six-second default every other caller has always had. Only the peer fetch differs. */
static char *http_get_ip(const char *ip, int port, const char *path, long *out_len) {
    return http_get_ip_to(ip, port, path, out_len, 6);
}

/* ---------------- console truth: app.db ----------------------------------
 * Everything the panels need — real names, sizes, regions, install location and
 * the installed VERSION — lives in tbl_contentinfo. Reading it here is what lets
 * the console app show the same information as the PC companion.
 * ------------------------------------------------------------------------- */
#define APP_DB_PATH "/system_data/priv/mms/app.db"
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
/* The console's firmware, as "13.60", or "unknown" when the kernel will not say.
 *
 * THIS IS THE FACT THAT DECIDES WHETHER THIS ELF RUNS AT ALL, and until now nothing reported it.
 * The payload SDK carries a switch on kernel_get_fw_version() that hands back the kernel offsets
 * for each firmware it knows, and -ENOSYS for one it does not - so a console that moves to a
 * firmware the build predates cannot establish kernel read/write and this program dies before it
 * reaches its own first log line. No process, no boot line, no notification: Payload Manager
 * reports a clean "Sent 45367592 bytes to loader" and nothing happens.
 *
 * That is exactly what the owner hit on 2026-10-01 after updating the console, and it cost an hour
 * of elimination - two of our builds, a 118 KB one of ours, and a third-party payload, all loaded
 * and all silent - to arrive at a number the console could simply have said. It says it now.
 *
 * Firmware is BCD: 0x13600000 is 13.60, so the bytes are printed as hex digits, not as decimals.
 */
static void fw_version_str(char *out, size_t osz) {
    uint32_t v = kernel_get_fw_version();
    if (!v) {
        snprintf(out, osz, "unknown");
        return;
    }
    snprintf(out, osz, "%x.%02x", (unsigned)((v >> 24) & 0xFF), (unsigned)((v >> 16) & 0xFF));
}


static void content_sig(char *out, size_t osz) {
    struct stat a, b;
    long long as = 0, am = 0, bs = 0, bm = 0;
    if (stat(APP_DB_PATH, &a) == 0) { as = (long long)a.st_size; am = (long long)a.st_mtime; }
    if (stat(ADDCONT_DB_PATH, &b) == 0) { bs = (long long)b.st_size; bm = (long long)b.st_mtime; }
    snprintf(out, osz, "%lld-%lld-%lld-%lld", as, am, bs, bm);
}

#define MAX_TITLES  256

typedef struct {
    char tid[16], cid[64], name[192], ver[24];
    long long size;
    int loc;                  /* 0 = internal, 2 = extended */
    char backup[400];         /* ShadowMount .ffpfsc, when there is one */
    long long backup_size;
    int has_backup;
} title_t;

typedef struct {
    title_t *rows;
    int n;
    int ci_tid, ci_cid, ci_name, ci_size, ci_loc, ci_json;
} titles_ctx_t;

/* PS4 and PS5 do NOT use the same field: CUSA -> APP_VER, PPSA -> CONTENT_VERSION.
   Reading only APP_VER is why every PS5 title used to report no version at all. */
static void appinfo_version(const char *aij, char *out, size_t outsz) {
    out[0] = 0;
    if (!aij) return;
    static const char *KEYS[2] = { "\"APP_VER\"", "\"CONTENT_VERSION\"" };
    for (int k = 0; k < 2; k++) {
        const char *p = strstr(aij, KEYS[k]);
        if (!p) continue;
        p = strchr(p + strlen(KEYS[k]), ':');
        if (!p) continue;
        p = strchr(p, '"');
        if (!p) continue;
        p++;
        const char *e = strchr(p, '"');
        if (!e || (size_t)(e - p) >= outsz) continue;
        memcpy(out, p, (size_t)(e - p));
        out[e - p] = 0;
        if (out[0]) return;
    }
}

static const char *region_of(const char *cid) {
    if (!cid || !cid[0]) return "—";
    switch (cid[0]) {
        case 'U': case 'u': return "US";
        case 'E': case 'e': return "EU";
        case 'J': case 'j': return "JP";
        case 'H': case 'h': return "ASIA";
        case 'K': case 'k': return "KR";
        case 'I': case 'i': return "INT";
        default: return "—";
    }
}

static void titles_cb(void *ctx, int ncol, char **v) {
    titles_ctx_t *c = (titles_ctx_t *)ctx;
    (void)ncol;
    if (c->n >= MAX_TITLES) return;
    const char *tid = v[c->ci_tid];
    if (strncmp(tid, "CUSA", 4) && strncmp(tid, "PPSA", 4)) return;   /* games only */
    title_t *t = &c->rows[c->n];
    memset(t, 0, sizeof(*t));
    snprintf(t->tid, sizeof(t->tid), "%s", tid);
    /* sq_col_index() answers -1 for a column that is not in this firmware's CREATE TABLE, and
       read_console_titles only insists on titleId/titleName/AppInfoJson. These three were indexed
       unguarded, so a schema that drops or renames one of them would have read v[-1] - the word
       before the calloc'd array - and handed it to snprintf. Absent means empty, not garbage. */
    snprintf(t->cid, sizeof(t->cid), "%s", c->ci_cid >= 0 ? v[c->ci_cid] : "");
    snprintf(t->name, sizeof(t->name), "%s", v[c->ci_name][0] ? v[c->ci_name] : tid);
    t->size = c->ci_size >= 0 ? strtoll(v[c->ci_size], NULL, 10) : 0;
    t->loc  = c->ci_loc  >= 0 ? atoi(v[c->ci_loc]) : 0;
    appinfo_version(v[c->ci_json], t->ver, sizeof(t->ver));
    c->n++;
}

/* Returns how many titles were read, or -1 when app.db could not be used. */
static int read_console_titles(title_t *rows, int max) {
    long len = 0;
    char *data = slurp(APP_DB_PATH, &len);
    if (!data || len < 512) { free(data); return -1; }
    sqdb_t db;
    if (sq_open(&db, (const uint8_t *)data, (size_t)len) != 0) { free(data); return -1; }
    sq_find_t fnd = { "tbl_contentinfo", 0, NULL };
    sq_walk(&db, 1, 5, sq_master_cb, &fnd, 0);
    if (!fnd.root) { free(fnd.sql); free(data); return -1; }
    titles_ctx_t c;
    memset(&c, 0, sizeof(c));
    c.rows = rows;
    c.ci_tid  = sq_col_index(fnd.sql, "titleId");
    c.ci_cid  = sq_col_index(fnd.sql, "contentId");
    c.ci_name = sq_col_index(fnd.sql, "titleName");
    c.ci_size = sq_col_index(fnd.sql, "size");
    c.ci_loc  = sq_col_index(fnd.sql, "contentLocation");
    c.ci_json = sq_col_index(fnd.sql, "AppInfoJson");
    if (c.ci_tid < 0 || c.ci_name < 0 || c.ci_json < 0) { free(fnd.sql); free(data); return -1; }
    int ncol = c.ci_json;
    if (c.ci_size > ncol) ncol = c.ci_size;
    if (c.ci_loc  > ncol) ncol = c.ci_loc;
    if (c.ci_cid  > ncol) ncol = c.ci_cid;
    ncol += 1;
    (void)max;
    sq_walk(&db, fnd.root, ncol, titles_cb, &c, 0);
    free(fnd.sql);
    free(data);
    return c.n;
}

/* app.db's `size` is a metadata figure, not the real one. The truth for these
   installs is the ShadowMount backup file, so pair each title with its .ffpfsc. */
static void attach_backups(title_t *rows, int n) {
    /* EVERY root, not just ext1. A backup on a USB drive used to be invisible here, which made
       its title report app.db's metadata size and left it with no backup_path. Both tiers are
       walked, and each keeps its own rule about what counts - so what the library shows as having a
       backup is exactly what the delete will be able to find again. */
    for (int tier = 0; tier < 2; tier++) {
      const char **roots = tier ? DRIVE_ROOTS : HOMEBREW_ROOTS;
      for (int r = 0; roots[r]; r++) {
        DIR *d = opendir(roots[r]);
        if (!d) continue;
        struct dirent *e;
        while ((e = readdir(d))) {
            const char *nm = e->d_name;
            if (strlen(nm) < 8 || nm[0] == '.') continue;
            if (tier && !is_backup_ext(nm)) continue;   /* drive roots: containers only */
            char tid[16];
            if (!find_tid(nm, tid)) continue;
            char cand[512];
            snprintf(cand, sizeof(cand), "%s/%s", roots[r], nm);
            /* Same rule as find_backup_for_tid: a container is known by its extension, a
               folder-shaped dump by what is inside it. Filtering on the name alone left a
               folder-backed title reporting has_backup=0 - so it showed app.db's metadata size and
               the app offered it the wrong delete. */
            if (!is_backup_ext(nm)) {
                struct stat cst;
                if (stat(cand, &cst) != 0 || !S_ISDIR(cst.st_mode)) continue;
                if (!looks_like_game_folder(cand)) continue;
            }
            for (int i = 0; i < n; i++) {
                if (strcmp(rows[i].tid, tid)) continue;
                if (rows[i].has_backup) break;          /* first root wins; do not overwrite */
                snprintf(rows[i].backup, sizeof(rows[i].backup), "%s", cand);
                struct stat st;
                rows[i].backup_size = (stat(rows[i].backup, &st) == 0) ? (long long)st.st_size : 0;
                rows[i].has_backup = 1;
                break;
            }
        }
        closedir(d);
      }
    }
}

/* Find the backup container for a title, across every homebrew root. Returns 1 and fills `out`.
   This is the ONLY way the delete endpoint learns a path - the caller never supplies one. */
static int find_backup_for_tid(const char *want_tid, char *out, size_t outsz, long long *size_out,
                               int *is_dir_out) {
    if (!want_tid || !want_tid[0]) return 0;
    if (is_dir_out) *is_dir_out = 0;
    /* Homebrew folders first: a game in a folder that exists to hold games is the normal case and
       should win. The drive roots are searched afterwards, at the tail of this function. */
    for (int r = 0; HOMEBREW_ROOTS[r]; r++) {
        DIR *d = opendir(HOMEBREW_ROOTS[r]);
        if (!d) continue;
        struct dirent *e;
        while ((e = readdir(d))) {
            const char *nm = e->d_name;
            if (strlen(nm) < 8) continue;
            if (nm[0] == '.') continue;                      /* . and .. */
            char tid[16];
            if (!find_tid(nm, tid)) continue;
            if (strcmp(tid, want_tid)) continue;
            char cand[512];
            snprintf(cand, sizeof(cand), "%s/%s", HOMEBREW_ROOTS[r], nm);
            /* A container is identified by its extension; a FOLDER-shaped dump has no extension at
               all and is identified by what is inside it. Filtering on the name alone made a folder
               backup invisible here - the endpoint answered "nothing to delete" while the tree sat
               in the watch folder and re-mounted on the next scan. */
            /* lstat, and a symlink is never a candidate. remove_tree() lstat()s its CHILDREN but
               this is where its root is chosen, and stat() resolved a link named like a dump to
               wherever it pointed - so a link inside a watch folder passed under_homebrew_root by
               its own name and the delete emptied the target's contents instead. */
            struct stat cst;
            if (lstat(cand, &cst) != 0) continue;
            if (S_ISLNK(cst.st_mode)) continue;
            if (!is_backup_ext(nm)) {
                if (!S_ISDIR(cst.st_mode)) continue;
                if (!looks_like_game_folder(cand)) continue;
            }
            snprintf(out, outsz, "%s", cand);
            if (size_out) *size_out = (long long)cst.st_size;
            if (is_dir_out) *is_dir_out = S_ISDIR(cst.st_mode) ? 1 : 0;
            closedir(d);
            return 1;
        }
        closedir(d);
    }
    /* TIER 2: the drive roots. Containers only, regular files only - see DRIVE_ROOTS. */
    for (int r = 0; DRIVE_ROOTS[r]; r++) {
        DIR *d = opendir(DRIVE_ROOTS[r]);
        if (!d) continue;
        struct dirent *e;
        while ((e = readdir(d))) {
            const char *nm = e->d_name;
            if (strlen(nm) < 8 || nm[0] == '.') continue;
            if (!is_backup_ext(nm)) continue;              /* no folder dumps at this tier */
            char tid[16];
            if (!find_tid(nm, tid)) continue;
            if (strcmp(tid, want_tid)) continue;
            char cand[512];
            snprintf(cand, sizeof(cand), "%s/%s", DRIVE_ROOTS[r], nm);
            struct stat cst;
            /* lstat: S_ISREG on a link's TARGET would let a link at a drive root pass as a file. */
            if (lstat(cand, &cst) != 0 || !S_ISREG(cst.st_mode)) continue;
            snprintf(out, outsz, "%s", cand);
            if (size_out) *size_out = (long long)cst.st_size;
            if (is_dir_out) *is_dir_out = 0;
            closedir(d);
            return 1;
        }
        closedir(d);
    }
    return 0;
}

/* Is `p` inside one of the folders that exist to hold games? The recursive delete below refuses to
   run anywhere else, so this is the last gate before anything is removed from a tree. */
static int under_homebrew_root(const char *p) {
    for (int r = 0; HOMEBREW_ROOTS[r]; r++) {
        size_t l = strlen(HOMEBREW_ROOTS[r]);
        if (!strncmp(p, HOMEBREW_ROOTS[r], l) && p[l] == '/' && p[l + 1]) return 1;
    }
    return 0;
}

/* Delete a folder-shaped game dump, contents and all.
 *
 * rmdir() only removes an EMPTY directory, so the old unlink-then-rmdir pair could never delete the
 * folder dumps it was written for: it failed with ENOTEMPTY and told the user the drive was
 * write-protected.
 *
 * This is the one piece of code in the project that removes a tree, so every bound is explicit:
 *   - it runs ONLY under a HOMEBREW_ROOTS entry, re-checked on entry and for every subdirectory;
 *   - it uses lstat and never follows a symlink, so a link inside the dump cannot lead it out;
 *   - it is depth-limited, so a link loop or a pathological tree cannot run away;
 *   - it never deletes a root itself, only things below one.
 * Returns 0 when the tree is gone. */
static int remove_tree(const char *path, int depth) {
    if (depth > 8) return -1;
    if (!under_homebrew_root(path)) return -1;
    DIR *d = opendir(path);
    if (!d) return -1;
    struct dirent *e;
    while ((e = readdir(d))) {
        const char *nm = e->d_name;
        if (!strcmp(nm, ".") || !strcmp(nm, "..")) continue;
        char child[700];
        if ((size_t)snprintf(child, sizeof(child), "%s/%s", path, nm) >= sizeof(child)) continue;
        struct stat st;
        if (lstat(child, &st) != 0) continue;          /* lstat: a symlink is removed, not followed */
        if (S_ISDIR(st.st_mode)) remove_tree(child, depth + 1);
        else unlink(child);
    }
    closedir(d);
    return rmdir(path);
}

/* Total bytes under a folder dump, so the "freed" figure means something for a folder too - a
   directory's own st_size is not the size of its contents. Same bounds, minus the deleting. */
static long long tree_bytes(const char *path, int depth) {
    if (depth > 8) return 0;
    DIR *d = opendir(path);
    if (!d) return 0;
    long long total = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        const char *nm = e->d_name;
        if (!strcmp(nm, ".") || !strcmp(nm, "..")) continue;
        char child[700];
        if ((size_t)snprintf(child, sizeof(child), "%s/%s", path, nm) >= sizeof(child)) continue;
        struct stat st;
        if (lstat(child, &st) != 0) continue;
        if (S_ISDIR(st.st_mode)) total += tree_bytes(child, depth + 1);
        else if (S_ISREG(st.st_mode)) total += (long long)st.st_size;
    }
    closedir(d);
    return total;
}


/* Peer library snapshot. Rebuilt at most every PEER_CACHE_MS, so a library request
   never opens sockets to every PC; it reuses the last answer. Less churn is also
   less to be caught mid-flight when the console suspends for rest mode. */
#define PEER_CACHE_MS 20000
static char  *g_peer_cache = NULL;
static size_t g_peer_cache_len = 0;
static long long g_peer_cache_ms = 0;
static pthread_mutex_t g_peer_cache_lock = PTHREAD_MUTEX_INITIALIZER;

static volatile int g_local_title_count = 0;   /* what the last library build emitted */

static void peer_cache_invalidate(void) {
    pthread_mutex_lock(&g_peer_cache_lock);
    free(g_peer_cache);
    g_peer_cache = NULL;
    g_peer_cache_len = 0;
    g_peer_cache_ms = 0;
    pthread_mutex_unlock(&g_peer_cache_lock);
}

/* Insert extra entries into one card's "updates"/"dlc" array, in place.
   The card was already written into the buffer by the local scan; the peer only
   supplies the add-ons the console does not have. */
static void splice_addons(char **buf, size_t *cap, size_t *len,
                          const char *tid, const char *arr_key, const char *items_json) {
    if (!items_json || !items_json[0] || !tid || !tid[0]) return;
    if (*len + 1 >= *cap) return;
    (*buf)[*len] = 0;                       /* strstr needs a terminator */

    char anchor[48];
    snprintf(anchor, sizeof(anchor), "\"title_id\":\"%s\"", tid);
    char *at = strstr(*buf, anchor);
    if (!at) return;
    char *stop = strstr(at, "\"cheats\":[]}");     /* end of this card */
    char *arr  = strstr(at, arr_key);
    if (!arr || (stop && arr > stop)) return;

    size_t ilen = strlen(items_json);
    size_t ins_off = (size_t)((arr + strlen(arr_key)) - *buf);   /* just past '[' */
    if (*len + ilen + 8 >= *cap) {
        size_t nc = *cap + ilen + 8192;
        char *nb = (char *)realloc(*buf, nc);
        if (!nb) return;
        *buf = nb; *cap = nc;
    }
    char *ins = *buf + ins_off;
    int comma = (*ins != ']');              /* array already has something in it */
    size_t tail = *len - ins_off;
    memmove(ins + ilen + (comma ? 1 : 0), ins, tail + 1);
    memcpy(ins, items_json, ilen);
    if (comma) ins[ilen] = ',';
    *len += ilen + (comma ? 1 : 0);
}

/* Add-ons a PC holds for a game the console already has. Remembered alongside the
   peer cache so a cached answer carries them too. */
/* These are spliced verbatim into the library JSON, so a truncated copy is a PARSE ERROR for
   the whole document - not a missing add-on. 900 was too small once a peer sends a game with
   more than about two DLC. */
typedef struct { char tid[16]; char upd[4096]; char dlc[4096]; } peer_addon_t;

/* This was a fixed array of 32 and it silently threw away everything past the 32nd title.
   That is not a corner case: peers are merged one after another, and the first peer alone held
   46 PS4 titles carrying a patch or DLC. It filled the table, and every title merged after it -
   which on this network meant EVERY PS5 title, because the PC holding them is merged second -
   got no add-ons at all. The symptom looked like a PS5 grouping bug: PS4 cards showed their
   updates and DLC, PS5 cards showed none, and nothing anywhere reported an error.
   So it grows on demand instead. The hard ceiling only exists so a malicious or broken peer
   cannot make us allocate without bound; at 8 KB per entry it is ~8 MB in the worst case, and
   nothing is allocated at all until a peer actually has add-ons to contribute. */
#define PEER_ADDON_HARD_MAX 1024
static peer_addon_t *g_peer_addons = NULL;
static int g_peer_addon_n = 0;
static int g_peer_addon_cap = 0;

/* A zeroed slot, or NULL if we are out of room. Never freed between requests: the splice pass
   runs on the cached path too, so the table has to outlive the merge that filled it. */
static peer_addon_t *peer_addon_new(void) {
    if (g_peer_addon_n >= PEER_ADDON_HARD_MAX) return NULL;
    if (g_peer_addon_n >= g_peer_addon_cap) {
        int nc = g_peer_addon_cap ? g_peer_addon_cap * 2 : 32;
        if (nc > PEER_ADDON_HARD_MAX) nc = PEER_ADDON_HARD_MAX;
        peer_addon_t *nb = (peer_addon_t *)realloc(g_peer_addons, (size_t)nc * sizeof(peer_addon_t));
        if (!nb) return NULL;                 /* keep the old table; we just stop growing */
        g_peer_addons = nb;
        g_peer_addon_cap = nc;
    }
    peer_addon_t *pa = &g_peer_addons[g_peer_addon_n++];
    memset(pa, 0, sizeof(*pa));
    return pa;
}

/* Pull one companion's advertised library and append anything we do not already have.
   `seen` holds the title ids already emitted so a game on two PCs stays one card.
   Returns 0 when the PC answered, -1 when it could not be reached - the caller uses that to stop
   asking a machine that is switched off. */
static int merge_pc_library(const char *ip, int port, char **buf, size_t *cap, size_t *len,
                            int *first, char seen[][16], int *nseen, int seen_max) {
    long blen = 0;
    /* THREE SECONDS, NOT SIX. This is the only remote fetch on the accept loop, and a PC that
       answered the handshake and then stopped talking holds the whole console for whatever this
       says. A federation document is a few hundred KB over a LAN; the timeout is per read, so a
       large one streaming steadily never trips it. */
    char *doc = http_get_ip_to(ip, port, "/api/federation", &blen, 3);
    if (!doc) return -1;
    char pcname[64] = {0};
    json_str_after(doc, "name", pcname, sizeof(pcname));
    int pccount = 0;
    long long pcbytes = 0;
    {
        const char *cp = strstr(doc, "\"count\"");
        if (cp) { cp = strchr(cp, ':'); if (cp) pccount = (int)strtol(cp + 1, NULL, 10); }
        /* How much this PC is holding, so it can appear on the storage bar with a real size. */
        const char *bp = strstr(doc, "\"bytes\"");
        if (bp) { bp = strchr(bp, ':'); if (bp) pcbytes = strtoll(bp + 1, NULL, 10); }
    }
    char pcver[16] = {0};
    json_str_after(doc, "version", pcver, sizeof(pcver));
    if (pcname[0] || pccount || pcver[0]) {   /* remember it: nicer than showing a bare address */
        pthread_mutex_lock(&g_pcs_lock);
        for (int i = 0; i < PC_MAX; i++)
            if (!strcmp(g_pcs[i].ip, ip)) {
                if (pcname[0]) snprintf(g_pcs[i].name, sizeof(g_pcs[i].name), "%s", pcname);
                if (pcver[0]) snprintf(g_pcs[i].ver, sizeof(g_pcs[i].ver), "%s", pcver);
                g_pcs[i].count = pccount;
                g_pcs[i].bytes = pcbytes;
                break;
            }
        pthread_mutex_unlock(&g_pcs_lock);
    }
    /* Peer-of-peer discovery: adopt the PCs this one can see. */
    {
        const char *kp = strstr(doc, "\"known_pcs\"");
        if (kp) {
            const char *q = kp;
            int d3 = 0;
            const char *ko = NULL;
            for (; *q; q++) {
                if (*q == '{') { if (d3 == 0) ko = q; d3++; }
                else if (*q == '}') {
                    d3--;
                    if (d3 == 0 && ko) {
                        char kip[24] = {0};
                        json_str_after(ko, "lan_ip", kip, sizeof(kip));
                        int kport = 0;
                        const char *pp = strstr(ko, "\"port\"");
                        if (pp) { pp = strchr(pp, ':'); if (pp) kport = (int)strtol(pp + 1, NULL, 10); }
                        if (kip[0] && kport > 0 && strcmp(kip, ip) != 0 &&
                            strcmp(kip, lan_ip_str()) != 0)
                            pc_register(kip, kport);
                        ko = NULL;
                    }
                } else if (*q == ']' && d3 == 0) break;
            }
        }
    }

    const char *garr = strstr(doc, "\"games\"");
    /* IT ANSWERED - it just had nothing to say. Not a connection failure, so it must not be backed
       off: a PC with an empty library would have disappeared from the console for a minute. */
    if (!garr) { free(doc); return 0; }

    const char *p = garr;
    int depth = 0;
    const char *obj = NULL;
    for (; *p; p++) {
        if (*p == '{') { if (depth == 0) obj = p; depth++; }
        else if (*p == '}') {
            depth--;
            if (depth == 0 && obj) {
                /* BOUNDED TO THIS ENTRY. "platform" chooses the install lane and "install_key" is
                   what an install is started with; an entry missing either used to take the next
                   entry's, which is a peer's game installed with another game's key. */
                char tid[24] = {0}, nm[240] = {0}, plat[8] = {0}, key[300] = {0};
                char icon[300] = {0};
                json_str_after_lim(obj, p, "title_id", tid, sizeof(tid));
                json_str_after_lim(obj, p, "name", nm, sizeof(nm));
                json_str_after_lim(obj, p, "platform", plat, sizeof(plat));
                json_str_after_lim(obj, p, "install_key", key, sizeof(key));
                json_str_after_lim(obj, p, "icon_url", icon, sizeof(icon));
                char thumb[300] = {0};
                json_str_after_lim(obj, p, "thumb_url", thumb, sizeof(thumb));
                /* An add-on with no base game must stay an add-on here too, or the console
                   shows it as a game and the Add-ons category never appears. */
                int upd_only = 0;
                {
                    const char *uo = strstr(obj, "\"update_only\"");
                    if (uo && uo < (obj + 4000)) {
                        const char *c2 = strchr(uo, ':');
                        if (c2 && strstr(c2, "true") && (strstr(c2, "true") - c2) < 4) upd_only = 1;
                    }
                }
                if (tid[0] && key[0]) {
                    int dup = 0;
                    for (int i = 0; i < *nseen; i++) if (!strcmp(seen[i], tid)) { dup = 1; break; }
                    {
                        long long sz = 0;
                        const char *sp = strstr(obj, "\"size\"");
                        if (sp) { sp = strchr(sp, ':'); if (sp) sz = strtoll(sp + 1, NULL, 10); }
                        char en[400], epc[128], eic[400];
                        json_escape(nm[0] ? nm : tid, en, sizeof(en));
                        json_escape(pcname[0] ? pcname : ip, epc, sizeof(epc));
                        json_escape(icon, eic, sizeof(eic));
                        char ethumb[400];
                        json_escape(thumb, ethumb, sizeof(ethumb));
                        /* Container type and version as the owner reports them. A ShadowMount backup
                           (.ffpfs / .ffpfsc / …) is not a PKG, and calling every peer title "PKG"
                           left a backup's panel showing no format at all — and, because the drive
                           picker only appears on the mount lane, left the console unable to choose
                           where a backup goes while the PCs could. Same lane as the owner reports. */
                        char pfmt[24] = {0}, pver[40] = {0};
                        json_str_after(obj, "format", pfmt, sizeof(pfmt));
                        json_str_after(obj, "version", pver, sizeof(pver));
                        if (!pfmt[0]) {
                            /* A PC on an older build states no format. Its filename is proof enough,
                               so a backup from a machine that has not been updated still reads right. */
                            const char *fq = strstr(obj, "\"file\"");
                            char pf[300] = {0};
                            if (fq) json_str_after(fq - 1, "file", pf, sizeof(pf));
                            if (!pf[0]) snprintf(pf, sizeof(pf), "%s", nm);
                            if (is_backup_ext(pf)) {
                                const char *d = strrchr(pf, '.');
                                if (d) snprintf(pfmt, sizeof(pfmt), "%s", d + 1);
                            }
                        }
                        char efmt[48], ever[80];
                        json_escape(pfmt[0] ? pfmt : "PKG", efmt, sizeof(efmt));
                        json_escape(pver, ever, sizeof(ever));
                        /* A backup mounts, so it gets the mount lane and with it the drive picker.
                           is_backup_ext() wants a filename, and pfmt is a bare extension. */
                        char fdot[40];
                        snprintf(fdot, sizeof(fdot), "x.%s", pfmt);
                        const char *plane = (pfmt[0] && (is_backup_ext(fdot) || !strcmp(pfmt, "folder")))
                                            ? "mount" : "install";

                        /* Build every bucket from the peer's item list. A game a PC holds as
                           base + update + DLC has to arrive here as all three, or its patches
                           and DLC are invisible on the console. */
                        char bkt[3][8192];   /* Jump Force alone has 21 DLC; 2200 truncated at ~2 */
                        int  bn[3] = {0, 0, 0};
                        for (int q = 0; q < 3; q++) bkt[q][0] = 0;
                        int any_base = 0;
                        const char *items = strstr(obj, "\"items\"");
                        if (items) {
                            const char *ip2 = items;
                            int d2 = 0;
                            const char *io = NULL;
                            for (; *ip2; ip2++) {
                                if (*ip2 == '{') { if (d2 == 0) io = ip2; d2++; }
                                else if (*ip2 == '}') {
                                    d2--;
                                    if (d2 == 0 && io) {
                                        char ik[300] = {0}, ikind[16] = {0}, ifile[300] = {0}, iver[40] = {0};
                                        json_str_after(io, "install_key", ik, sizeof(ik));
                                        json_str_after(io, "kind", ikind, sizeof(ikind));
                                        json_str_after(io, "file", ifile, sizeof(ifile));
                                        json_str_after(io, "version", iver, sizeof(iver));
                                        long long isz = 0;
                                        const char *sp2 = strstr(io, "\"size\"");
                                        if (sp2) { sp2 = strchr(sp2, ':'); if (sp2) isz = strtoll(sp2 + 1, NULL, 10); }
                                        if (ik[0]) {
                                            int bi = !strcmp(ikind, "dlc") ? 2
                                                   : !strcmp(ikind, "update") ? 1 : 0;
                                            /* Two PCs holding the same patch must not list it twice. */
                                            if (dup && bi != 0) {
                                                char probe[340];
                                                snprintf(probe, sizeof(probe), "\"install_key\":\"%s\"", ik);
                                                int already = 0;
                                                if (*len + 1 < *cap) {
                                                    (*buf)[*len] = 0;
                                                    if (strstr(*buf, probe)) already = 1;
                                                }
                                                for (int z = 0; !already && z < g_peer_addon_n; z++)
                                                    if (strstr(g_peer_addons[z].upd, probe) ||
                                                        strstr(g_peer_addons[z].dlc, probe)) already = 1;
                                                if (already) { io = NULL; continue; }
                                            }
                                            if (bi == 0) any_base = 1;
                                            char eik[400], eif[400];
                                            json_escape(ik, eik, sizeof(eik));
                                            json_escape(ifile[0] ? ifile : ik, eif, sizeof(eif));
                                            /* Build the item WHOLE, then append only if it fits.
                                               This used to snprintf straight into the bucket behind a
                                               600-byte headroom guard - but one item can exceed 900
                                               bytes (two 400-char escaped keys plus the URL), so it
                                               passed the guard and then truncated MID-VALUE. The
                                               result was JSON like
                                                 "peer_url":"http://10.0.0.76:8710/li],"cheats":[]}
                                               which is a parse error, so the console UI got nothing
                                               back from /api/library and showed an empty library
                                               while the PC UI was fine. A partial item is never
                                               acceptable: skip it instead, and only count it in
                                               bn[bi] (the comma logic) when it was really written. */
                                            char item[1200];
                                            int iw = snprintf(item, sizeof(item),
                                                    "%s{\"file\":\"%s\",\"size\":%lld,\"kind\":\"%s\","
                                                    "\"version\":\"%s\",\"install_key\":\"%s\","
                                                    "\"peer_url\":\"http://%s:%d/library/%s\"}",
                                                    bn[bi] ? "," : "", eif, isz,
                                                    ikind[0] ? ikind : "base", iver, eik, ip, port, eik);
                                            size_t bl = strlen(bkt[bi]);
                                            if (iw > 0 && (size_t)iw < sizeof(item) &&
                                                bl + (size_t)iw + 1 < sizeof(bkt[bi])) {
                                                memcpy(bkt[bi] + bl, item, (size_t)iw + 1);
                                                bn[bi]++;
                                            }
                                        }
                                        io = NULL;
                                    }
                                } else if (*ip2 == ']' && d2 == 0) break;
                            }
                        }
                        if (!bn[0] && !bn[1] && !bn[2]) {
                            /* An older peer that only sends one key: keep working with it. */
                            char eik[400];
                            json_escape(key, eik, sizeof(eik));
                            snprintf(bkt[0], sizeof(bkt[0]),
                                "{\"file\":\"%s\",\"size\":%lld,\"kind\":\"base\","
                                "\"install_key\":\"%s\",\"peer_url\":\"http://%s:%d/library/%s\"}",
                                en, sz, eik, ip, port, eik);
                            bn[0] = 1;
                            any_base = 1;
                        }

                        if (dup) {
                            /* The console already shows this game, so the card stays as it is -
                               but the PC may hold its patch or its DLC, and dropping those is the
                               whole reason updates sitting on a PC never appeared here. Remember
                               them; they are spliced into the card once the peer block is final. */
                            peer_addon_t *pa = (bn[1] || bn[2]) ? peer_addon_new() : NULL;
                            if (pa) {
                                snprintf(pa->tid, sizeof(pa->tid), "%s", tid);
                                /* Copy only if it fits WHOLE. A partial copy would be spliced into
                                   the document and break the JSON for every title, not just this
                                   one - better to lose one peer's add-on list than the library. */
                                const char *su = bn[1] ? bkt[1] : "", *sd = bn[2] ? bkt[2] : "";
                                pa->upd[0] = pa->dlc[0] = 0;
                                if (strlen(su) < sizeof(pa->upd)) memcpy(pa->upd, su, strlen(su) + 1);
                                if (strlen(sd) < sizeof(pa->dlc)) memcpy(pa->dlc, sd, strlen(sd) + 1);
                            }
                            obj = NULL;
                            continue;
                        }
                        if (*nseen < seen_max) snprintf(seen[(*nseen)++], 16, "%s", tid);

                        /* snprintf returns what it WOULD have written, not what it did. Adding
                           that straight onto *len let *len pass *cap on a long record — and then
                           `*cap - *len` (both size_t) wrapped to a huge number, the headroom test
                           below passed, and the next record wrote past the end of the heap block
                           with an effectively unlimited size. One PC with long game names was
                           enough. Reserve first, write into the real space, and only advance by
                           what actually landed. */
                        {
                            size_t avail = (*cap > *len) ? (*cap - *len) : 0;
                            if (avail < 16384) {
                                size_t nc = (*cap * 2 > *len + 32768) ? *cap * 2 : *len + 32768;
                                char *nb2 = (char *)realloc(*buf, nc);
                                if (!nb2) { free(doc); return 0; }  /* our memory, not its fault */
                                *buf = nb2; *cap = nc;
                            }
                        }
                        size_t avail = *cap - *len;
                        int wrote = snprintf(*buf + *len, avail,
                            "%s{\"title_id\":\"%s\",\"name\":\"%s\",\"platform\":\"%s\","
                            "\"region\":\"\\u2014\",\"size\":%lld,\"size_known\":%s,"
                            "\"on_console\":false,\"lane\":\"%s\",\"format\":\"%s\","
                            "\"version\":\"%s\","
                            "\"has_icon\":%s,\"icon_url\":\"%s\",\"thumb_url\":\"%s\",\"cover_seed\":\"%s\","
                            "\"remote\":true,\"update_only\":%s,"
                            "\"source_pc\":\"%s\",\"hosts\":[{\"pc\":\"%s\",\"local\":false}],"
                            "\"base\":[%s],\"updates\":[%s],\"dlc\":[%s],\"cheats\":[]}",
                            *first ? "" : ",", tid, en, plat[0] ? plat : "PS4", sz,
                            sz > 0 ? "true" : "false",
                            plane, efmt, ever,
                            icon[0] ? "true" : "false", eic, ethumb, tid,
                            (upd_only || !any_base) ? "true" : "false", epc, epc,
                            bkt[0], bkt[1], bkt[2]);
                        if (wrote < 0 || (size_t)wrote >= avail) {
                            /* Did not fit even after reserving 16 KB — drop this one title rather
                               than carry a half-written record into the document. */
                            (*buf)[*len] = 0;
                        } else {
                            *len += (size_t)wrote;
                            *first = 0;
                        }
                    }
                }
                obj = NULL;
            }
        } else if (*p == ']' && depth == 0) break;
    }
    free(doc);
    return 0;
}

static char *build_library_json(void) {
    char pcbase[64] = {0};
    best_pc_base(pcbase, sizeof(pcbase));
    size_t cap = 1 << 16, len = 0;
    char *buf = malloc(cap);
    if (!buf) return NULL;
    #define APPEND(...) do { \
        if (cap - len < 2048) { cap *= 2; char *nb = realloc(buf, cap); if (!nb) { free(buf); return NULL; } buf = nb; } \
        len += snprintf(buf + len, cap - len, __VA_ARGS__); \
    } while (0)

    long long ext_used = 0; int ext_count = 0;
    int int_count = 0;            /* titles registered on internal storage */
    APPEND("{\"on_console_server\":true,\"console_reachable\":true,\"games\":[");
    int first = 1;

    /* Installed titles straight from app.db: real names, sizes, regions, install
       location and the installed version — the same source the PC companion uses,
       so the console app shows exactly the same information with the PC off. */
    title_t *rows = (title_t *)calloc(MAX_TITLES, sizeof(title_t));
    int nt = rows ? read_console_titles(rows, MAX_TITLES) : -1;
    if (nt > 0) {
        attach_backups(rows, nt);
        for (int i = 0; i < nt; i++) {
            title_t *t = &rows[i];
            int is_ps5 = !strncmp(t->tid, "PPSA", 4);
            long long real = t->has_backup && t->backup_size > 0 ? t->backup_size : t->size;
            const char *drive = (t->loc == 2) ? "Extended Storage" : "Internal SSD";
            if (t->loc == 2) { ext_used += real; ext_count++; } else int_count++;
            char en[400], eb[600];
            json_escape(t->name, en, sizeof(en));
            json_escape(t->backup, eb, sizeof(eb));
            /* Report the container this title actually has, not the one format we happened to name.
               Every backup was labelled "ffpfsc", so an .ffpfs or .exfat dump described itself wrongly. */
            char bfmt[16] = "ffpfsc";
            if (t->has_backup) {
                const char *bd = strrchr(t->backup, '.');
                if (bd && bd[1]) snprintf(bfmt, sizeof(bfmt), "%s", bd + 1);
            }
            /* The console has no image library, so card-sized art can only come from a
               companion. Point at the best one; it generates and caches the thumbnail. */
            char tthumb[220] = {0};
            if (pcbase[0] && icon_exists(t->tid))
                snprintf(tthumb, sizeof(tthumb), "%s/thumb/%s.webp", pcbase, t->tid);
            APPEND("%s{\"title_id\":\"%s\",\"content_id\":\"%s\",\"name\":\"%s\","
                   "\"platform\":\"%s\",\"region\":\"%s\",\"size\":%lld,\"console_size\":%lld,"
                   "\"size_known\":%s,\"on_console\":true,\"installed_drive\":\"%s\","
                   "\"installed_version\":\"%s\",\"lane\":\"installed\",\"format\":\"%s\","
                   "\"has_icon\":%s,\"thumb_url\":\"%s\",\"cover_seed\":\"%s\",\"source\":\"%s\"",
                   first ? "" : ",", t->tid, t->cid, en,
                   is_ps5 ? "PS5" : "PS4", region_of(t->cid), real, real,
                   real > 0 ? "true" : "false", drive, t->ver,
                   /* format comes BEFORE has_icon in the string above - keep them in that order. */
                   t->has_backup ? bfmt : (is_ps5 ? "PS5 app" : "PS4 app"),
                   /* has_icon was hardcoded true for EVERY app.db title. The UI only emits an <img>
                      when it is set, so claiming art that is not there made each such card fire a
                      request that 404s - one per card, serialised through this accept loop. */
                   icon_exists(t->tid) ? "true" : "false", tthumb,
                   t->tid, t->has_backup ? "backup" : "installed");
            /* A backup has a real file behind it, so say what can be done with that file. The
               UI shows "Move to another drive" only for a title with `movable` AND a path
               (web/index.html), and without these two an installed backup had no way to reach
               another drive at all when no PC was on the network - its `base` is empty here, so
               there was no Install route either. The stick scan below has always sent them. */
            if (t->has_backup)
                APPEND(",\"backup_path\":\"%s\",\"local_path\":\"%s\","
                       "\"movable\":true,\"runs_in_place\":true", eb, eb);
            APPEND(",\"base\":[],\"updates\":[],\"dlc\":[],\"cheats\":[]}");
            first = 0;
        }
    }
    /* rows stays alive: the removable scan below needs it to avoid listing a
       backup that is already registered as installed. */

    /* Removable media. This is how the app is meant to work with no PC at all.
       Two different things live on a stick and they are NOT interchangeable:
         .pkg                    -> a package that must be INSTALLED. A PS4 title
                                    cannot run from removable media.
         .ffpfsc/.ffpfs/.ffpkg   -> a ShadowMount backup, which runs in place from
                                    any drive ShadowMount scans, USB included.
       Both are listed, each tagged with the drive it actually sits on. */
    for (int u = 0; u < 10 && len < cap; u++) {
        char root[64], label[32];
        if (u < 8) { snprintf(root, sizeof(root), "/mnt/usb%d", u); snprintf(label, sizeof(label), "USB %d", u); }
        else       { snprintf(root, sizeof(root), "/mnt/ext%d", u - 8); snprintf(label, sizeof(label), "Extended %d", u - 8); }
        /* Skip a drive that is not actually mounted: an unmounted /mnt/usbN still
           opens fine because it resolves to the parent filesystem. */
        {
            struct stat ds, ms;
            if (stat(root, &ds) != 0) continue;
            if (stat("/mnt", &ms) == 0 && ds.st_dev == ms.st_dev) continue;
        }
        /* ShadowMount scans the drive root AND <drive>/homebrew, so look in both. */
        for (int pass = 0; pass < 3; pass++) {
            char dir[160];
            if (pass == 0)      snprintf(dir, sizeof(dir), "%s", root);
            else if (pass == 1) snprintf(dir, sizeof(dir), "%s/homebrew", root);
            else                snprintf(dir, sizeof(dir), "%s/pkg", root);
            DIR *ud = opendir(dir);
            if (!ud) continue;
            struct dirent *e;
            while ((e = readdir(ud)) && len < cap) {
                const char *nm = e->d_name;
                if (nm[0] == '.') continue;
                int is_pkg = path_ext_is(nm, ".pkg");
                int is_bak = is_backup_ext(nm);
                if (!is_pkg && !is_bak) continue;
                char full[400];
                snprintf(full, sizeof(full), "%s/%s", dir, nm);
                struct stat st;
                if (stat(full, &st) != 0 || !S_ISREG(st.st_mode)) continue;
                char tid[16];
                if (!find_tid(nm, tid)) continue;      /* no CUSA/PPSA id -> not ours */
                /* Already registered on the console? Then it is the SAME game we
                   listed from app.db - listing the file again would duplicate it. */
                int dup = 0;
                for (int q = 0; q < nt; q++) if (!strcmp(rows[q].tid, tid)) { dup = 1; break; }
                if (dup) continue;
                int is_ps5 = !strncmp(tid, "PPSA", 4);
                char en[400], ef[600], hn[420];
                json_escape(nm, en, sizeof(en));
                json_escape(full, ef, sizeof(ef));
                /* Prefer the human part of a backup name: "[PS5] PPSA... - Name.ffpfsc" */
                const char *dash = strstr(nm, " - ");
                if (is_bak && dash) {
                    char tmp[300];
                    snprintf(tmp, sizeof(tmp), "%s", dash + 3);
                    char *dot = strrchr(tmp, '.');
                    if (dot) *dot = 0;
                    json_escape(tmp, hn, sizeof(hn));
                } else {
                    snprintf(hn, sizeof(hn), "%s", en);
                }
                APPEND("%s{\"title_id\":\"%s\",\"name\":\"%s\",\"platform\":\"%s\",",
                       first ? "" : ",", tid, hn, is_ps5 ? "PS5" : "PS4");
                APPEND("\"region\":\"\\u2014\",\"size\":%lld,\"size_known\":true,"
                       "\"on_console\":false,\"lane\":\"%s\",\"format\":\"%s\","
                       "\"has_icon\":false,\"cover_seed\":\"%s\","
                       "\"source\":\"%s\",\"source_label\":\"%s\",\"removable\":true,"
                       "\"local_path\":\"%s\",\"runs_in_place\":%s,\"movable\":true,",
                       (long long)st.st_size, is_bak ? "mount" : "install",
                       is_bak ? "FFPFSC" : "PKG", tid,
                       root + 5, label, ef,
                       is_bak ? "true" : "false");
                APPEND("\"base\":[{\"file\":\"%s\",\"path\":\"%s\",\"size\":%lld,"
                       "\"kind\":\"base\",\"install_key\":\"local:%s\",\"local\":true,"
                       "\"drive\":\"%s\"}],"
                       "\"updates\":[],\"dlc\":[],\"cheats\":[]}",
                       en, ef, (long long)st.st_size, ef, label);
                first = 0;
            }
            closedir(ud);
        }
    }

    free(rows);

    /* Games held by every PC that has announced itself. The console shows the SAME library
       no matter which computer happens to be open, and keeps working when they are all off. */
    {
        /* Serve the cached peer block when it is fresh. */
        long long nowms = now_ms_local();
        pthread_mutex_lock(&g_peer_cache_lock);
        int fresh = (g_peer_cache && (nowms - g_peer_cache_ms) < PEER_CACHE_MS);
        if (fresh) {
            if (cap - len < g_peer_cache_len + 64) {
                size_t nc = cap + g_peer_cache_len + 4096;
                char *nb = (char *)realloc(buf, nc);
                if (nb) { buf = nb; cap = nc; }
            }
            if (cap - len > g_peer_cache_len + 8) {
                if (g_peer_cache_len) {
                    memcpy(buf + len, g_peer_cache, g_peer_cache_len);
                    len += g_peer_cache_len;
                    first = 0;
                }
            } else fresh = 0;
        }
        pthread_mutex_unlock(&g_peer_cache_lock);
        if (fresh) goto peers_done;
    }
    {
        size_t peer_start = len;
        int    peer_first = first;
        static char seen[192][16];
        int nseen = 0;
        for (int i = 0; i < len && nseen < 192; i++) { (void)i; break; }
        /* seed `seen` with what we already emitted so a PC copy never duplicates a card */
        const char *scan = buf;
        while ((scan = strstr(scan, "\"title_id\":\"")) != NULL && nseen < 192) {
            scan += 12;
            const char *e2 = strchr(scan, '"');
            if (!e2) break;
            size_t l2 = (size_t)(e2 - scan);
            if (l2 < 16) { memcpy(seen[nseen], scan, l2); seen[nseen][l2] = 0; nseen++; }
            scan = e2 + 1;
        }
        pcpeer_t local_copy[PC_MAX];
        pthread_mutex_lock(&g_pcs_lock);
        memcpy(local_copy, g_pcs, sizeof(local_copy));
        pthread_mutex_unlock(&g_pcs_lock);
        g_peer_addon_n = 0;
        /* THIS LOOP IS THE ONLY PLACE THE ACCEPT LOOP TALKS TO ANOTHER MACHINE, so it is the only
           place that can make the whole console stop answering. It used to dial every entry it had
           ever been told about, with no deadline on connect() and no regard for whether the PC had
           been heard from this year - and the first entry being a switched-off laptop was enough to
           take the shop down completely. Every other reader of this table already skipped the stale
           ones; this one, the only one that actually opens a socket, did not. */
        long long merge_t0 = now_ms_local();
        for (int i = 0; i < PC_MAX; i++) {
            if (!local_copy[i].ip[0]) continue;
            long long nowpc = now_ms_local();
            if (nowpc - local_copy[i].last_ms > PC_DIAL_STALE_MS) continue;  /* quiet: do not dial */
            if (local_copy[i].fail_ms &&
                nowpc - local_copy[i].fail_ms < PC_FAIL_BACKOFF_MS) continue;   /* just refused */
            if (nowpc - merge_t0 > PEER_MERGE_BUDGET_MS) {
                ilog("library: stopped merging after %lldms - %d companion(s) not reached",
                     nowpc - merge_t0, PC_MAX - i);
                break;
            }
            if (merge_pc_library(local_copy[i].ip, local_copy[i].port,
                                 &buf, &cap, &len, &first, seen, &nseen, 192) != 0) {
                /* Write the failure back where the next request will see it. Matched on ip AND
                   port, because the slot may have been reused by another PC in the meantime. */
                pthread_mutex_lock(&g_pcs_lock);
                for (int k = 0; k < PC_MAX; k++) {
                    if (g_pcs[k].port == local_copy[i].port &&
                        !strcmp(g_pcs[k].ip, local_copy[i].ip)) { g_pcs[k].fail_ms = now_ms_local(); break; }
                }
                pthread_mutex_unlock(&g_pcs_lock);
            }
        }
        /* Keep exactly what we just appended, so the next request costs nothing. */
        pthread_mutex_lock(&g_peer_cache_lock);
        free(g_peer_cache);
        g_peer_cache_len = len - peer_start;
        g_peer_cache = (char *)malloc(g_peer_cache_len + 1);
        if (g_peer_cache) {
            memcpy(g_peer_cache, buf + peer_start, g_peer_cache_len);
            g_peer_cache[g_peer_cache_len] = 0;
            g_peer_cache_ms = now_ms_local();
        } else g_peer_cache_len = 0;
        pthread_mutex_unlock(&g_peer_cache_lock);
        (void)peer_first;
    }
peers_done:
    /* Hand the console's own cards the patches and DLC the PCs are holding for them.
       Done last, so the offsets used to cache the peer block above stay valid. */
    {   /* count the cards we just built, so the Devices panel can show a real number */
        int n_cards = 0;
        if (len + 1 < cap) {
            buf[len] = 0;
            const char *c3 = buf;
            while ((c3 = strstr(c3, "\"title_id\":\"")) != NULL) { n_cards++; c3 += 12; }
        }
        g_local_title_count = n_cards;
    }
    for (int a = 0; a < g_peer_addon_n; a++) {
        if (g_peer_addons[a].upd[0])
            splice_addons(&buf, &cap, &len, g_peer_addons[a].tid, "\"updates\":[", g_peer_addons[a].upd);
        if (g_peer_addons[a].dlc[0])
            splice_addons(&buf, &cap, &len, g_peer_addons[a].tid, "\"dlc\":[", g_peer_addons[a].dlc);
    }

    /* Real capacity from the filesystem — reporting null made the UI show "—". */
    long long i_tot = 0, i_free = 0, e_tot = 0, e_free = 0;
    struct statvfs vfs;
    if (statvfs("/user", &vfs) == 0) {
        i_tot  = (long long)vfs.f_blocks * (long long)vfs.f_frsize;
        i_free = (long long)vfs.f_bavail * (long long)vfs.f_frsize;
    }
    if (statvfs("/mnt/ext1", &vfs) == 0) {
        e_tot  = (long long)vfs.f_blocks * (long long)vfs.f_frsize;
        e_free = (long long)vfs.f_bavail * (long long)vfs.f_frsize;
    }
    APPEND("],\"drives\":[{\"id\":\"internal\",\"label\":\"Internal SSD\",\"used\":%lld,\"free\":%lld,"
           "\"total\":%lld,\"count\":%d,\"kind\":\"console\"}",
           (i_tot - i_free), i_free, i_tot, int_count);
    /* Extended storage only when there IS some. With no drive attached statvfs reports nothing and
       this became an empty tile claiming a drive that is not plugged in. */
    if (e_tot > 0 || ext_count > 0)
        APPEND(",{\"id\":\"ext1\",\"label\":\"Extended Storage\",\"used\":%lld,\"free\":%lld,"
               "\"total\":%lld,\"count\":%d,\"kind\":\"console\"}",
               (e_tot > 0 ? (e_tot - e_free) : ext_used), e_free, e_tot, ext_count);
    /* The PCs are storage too: that is where the games being installed actually live. Listing them
       beside the console's own drives is what makes every device show the same picture. */
    {
        pcpeer_t pcs[PC_MAX];
        pthread_mutex_lock(&g_pcs_lock);
        memcpy(pcs, g_pcs, sizeof(pcs));
        pthread_mutex_unlock(&g_pcs_lock);
        for (int i = 0; i < PC_MAX; i++) {
            if (!pcs[i].ip[0] || pcs[i].count <= 0) continue;
            char enm[140];
            json_escape(pcs[i].name[0] ? pcs[i].name : pcs[i].ip, enm, sizeof(enm));
            APPEND(",{\"id\":\"pc:%s\",\"label\":\"%s\",\"used\":%lld,\"free\":null,\"total\":null,"
                   "\"count\":%d,\"kind\":\"pc\",\"lan_ip\":\"%s\"}",
                   pcs[i].ip, enm, pcs[i].bytes, pcs[i].count, pcs[i].ip);
        }
    }
    APPEND("]}");
    #undef APPEND
    return buf;
}

/* /api/installed = just the title ids from the same scan */
static char *build_installed_json(void) {
    /* Straight from app.db. This used to scrape every "title_id" out of the library
       json, so a package merely SITTING on a USB stick came back as installed and
       its card wrongly showed the INSTALLED badge. */
    title_t *rows = (title_t *)calloc(MAX_TITLES, sizeof(title_t));
    int n = rows ? read_console_titles(rows, MAX_TITLES) : -1;
    size_t cap = 4096, len = 0;
    char *out = (char *)malloc(cap);
    if (!out) { free(rows); return NULL; }
    len += snprintf(out + len, cap - len, "{\"installed\":[");
    int first = 1;
    for (int i = 0; i < n && cap - len > 64; i++) {
        len += snprintf(out + len, cap - len, "%s\"%s\"", first ? "" : ",", rows[i].tid);
        first = 0;
    }
    snprintf(out + len, cap - len, "],\"source\":\"console\",\"console_reachable\":true}");
    free(rows);
    return out;
}

/* serve GET /api/icon/<TID> from the console's real appmeta/app icon */
/* Does this title actually have artwork on disk? Same two paths send_icon() serves. */
/* Base URL of the companion we would use, e.g. "http://10.0.0.76:8710", or "" when none is known.
   Ranked exactly like /api/companion: newest build first, then most recently seen. */
static void best_pc_base(char *out, size_t outsz) {
    out[0] = 0;
    pcpeer_t pcs[PC_MAX];
    pthread_mutex_lock(&g_pcs_lock);
    memcpy(pcs, g_pcs, sizeof(pcs));
    pthread_mutex_unlock(&g_pcs_lock);
    int best = -1;
    long long now_pc = now_ms_local();
    for (int i = 0; i < PC_MAX; i++) {
        if (!pcs[i].ip[0]) continue;
        if (now_pc - pcs[i].last_ms > PC_STALE_MS) continue;   /* gone quiet - do not send anyone there */
        if (best < 0) { best = i; continue; }
        int c = ver_cmp(pcs[i].ver, pcs[best].ver);
        if (c > 0 || (c == 0 && pcs[i].last_ms > pcs[best].last_ms)) best = i;
    }
    if (best >= 0) snprintf(out, outsz, "http://%s:%d", pcs[best].ip, pcs[best].port);
}

static int icon_exists(const char *tid) {
    char path[512];
    struct stat st;
    if (!tid || !tid[0]) return 0;
    snprintf(path, sizeof(path), "%s/%s/icon0.png", APPMETA_DIR, tid);
    if (stat(path, &st) == 0 && S_ISREG(st.st_mode)) return 1;
    snprintf(path, sizeof(path), "/user/app/%s/sce_sys/icon0.png", tid);
    return stat(path, &st) == 0 && S_ISREG(st.st_mode);
}

/* Where a companion leaves card-sized covers for us. One JPEG per title id.
   Spelled out rather than built from SHOP_DATA_DIR, which this file does not define until well
   below here; the static assertion beside that define keeps the two from drifting apart. */
#define THUMB_CACHE_DIR  "/data/pkg-mutant-shop/thumbs"

/* Does this console hold a prepared thumbnail for a title? */
static int thumb_exists(const char *tid) {
    char p[512];
    struct stat st;
    if (!tid || !tid[0]) return 0;
    snprintf(p, sizeof(p), "%s/%s.jpg", THUMB_CACHE_DIR, tid);
    return stat(p, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0;
}

static int send_icon(int fd, const char *tid);

/* GET /thumb/<TID>.jpg - the small cover if we have one, the original if we do not.

   THE FALLBACK IS THE WHOLE POINT. The page asks for this URL for every card, and a 404 here
   would run the cover's error path and leave initials in its place - permanently, for that card,
   until the grid is rebuilt. Answering with the full-size icon instead means asking is always
   safe: a console with a filled cache is fast, one without behaves exactly as it did before. */
static int send_thumb(int fd, const char *tid) {
    char p[512];
    struct stat st;
    snprintf(p, sizeof(p), "%s/%s.jpg", THUMB_CACHE_DIR, tid);
    if (stat(p, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0
        && send_file(fd, p) == 0) return 0;
    return send_icon(fd, tid);
}

static int send_icon(int fd, const char *tid) {
    char path[512];
    struct stat st;
    snprintf(path, sizeof(path), "%s/%s/icon0.png", APPMETA_DIR, tid);
    if (stat(path, &st) == 0 && send_file(fd, path) == 0) return 0;
    snprintf(path, sizeof(path), "/user/app/%s/sce_sys/icon0.png", tid);
    if (send_file(fd, path) == 0) return 0;
    return -1;
}

/* ================= OUR OWN PKG DOWNLOADER + INSTALLER =======================
 * End-to-end and self-contained: stream the PKG to local storage ourselves, then
 * register it with sceAppInstUtilAppInstallPkg — the one call proven to work
 * from this process (it is what installs our own tile).
 *
 * We deliberately do NOT use sceAppInstUtilInstallByPackage(url): it returns
 * 0x80B2116F here (see NOTES.md for the full investigation). Downloading ourselves owns
 * progress, resume and placement instead of guessing at a black box.
 *
 * Plain HTTP only — that is exactly what the companion serves, and it keeps this
 * dependency-free (no TLS stack in a payload).
 * ------------------------------------------------------------------------- */
static void mkparents(const char *path);   /* defined with the web self-extractor below */
/* connect to 127.0.0.1:port with send/recv timeouts; fd, or -1 if nothing is listening */
static int connect_local(int port, int rcv_ms, int snd_ms) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return -1;
    struct timeval tv;
    tv.tv_sec = rcv_ms / 1000; tv.tv_usec = (rcv_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    tv.tv_sec = snd_ms / 1000; tv.tv_usec = (snd_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)port);
    a.sin_addr.s_addr = htonl(INADDR_LOOPBACK);
    /* Loopback refuses instantly when nothing is listening, so this one was never the problem -
       bounded anyway, so that no outbound connect in this file is left to the stack's own schedule. */
    if (connect_deadline(s, (struct sockaddr *)&a, sizeof(a), CONNECT_MS) != 0) { close(s); return -1; }
    return s;
}

/* The install service runs sandboxed and cannot see /data/... — it sees the same files under
   /user/data/... . URLs pass through untouched. */
static void rewrite_for_install(const char *in, char *out, size_t outsz) {
    if (!strncmp(in, "/data/", 6)) snprintf(out, outsz, "/user%s", in);
    else                           snprintf(out, outsz, "%s", in);
}

#define DL_DIR_DEFAULT "/data/pkg-mutant-shop/install"
/* The download side's own buffer. Same reasoning and the same measured knee as COPY_BUF_BYTES:
   at 256 KB this path makes four times the read()/write() calls of a 1 MB one for the same bytes,
   and on this kernel that is throughput. */
#define DL_BUF         (1 << 20)

enum { JOB_IDLE = 0, JOB_DOWNLOAD, JOB_INSTALL, JOB_DONE, JOB_ERROR };

typedef struct {
    int       id;                 /* every job gets an identity so a caller can never
                                     mistake the PREVIOUS job's "done" for its own */
    int       state;
    int       cancel;
    long long total;
    long long done;
    int       rc;
    char      url[1200];
    char      path[640];
    char      name[160];
    char      msg[220];
    char      content_id[64];
    /* FETCH-ONLY: download and stop. A PS5 backup is mounted by ShadowMount, not installed, so the
       install half of dl_worker must not run for it. `final` is where the finished file is renamed
       to - we download to "<final>.part" because ShadowMount mounts the instant a file appears in
       its watch folder, and a half-written container mounts as a broken game. */
    int       fetch_only;
    char      final[640];
    /* What the release said this file's sha256 is, when it said. Checked before the file is put
       in place, never after. */
    char      digest[72];
    /* What was fetched, so the sentence on the television is about the right thing: "" for a
       console backup (the original caller), "file" for anything else. */
    char      purpose[16];
    /* Remove any other file in the destination with the same payload stem before putting this
       one in place, so a folder cannot end up holding two versions of one payload. */
    int       stem_clean;
    /* The release this file came from, written beside it when the transfer finishes so the panel
       can stop offering an update that has already been taken. */
    char      tag[120];
} dl_job_t;

static dl_job_t        g_job;
static int             g_job_seq = 0;
static pthread_mutex_t g_job_mtx = PTHREAD_MUTEX_INITIALIZER;

static void job_set(int state, const char *msg) {
    pthread_mutex_lock(&g_job_mtx);
    g_job.state = state;
    if (msg) snprintf(g_job.msg, sizeof(g_job.msg), "%s", msg);
    pthread_mutex_unlock(&g_job_mtx);
}

/* split "http://host[:port]/path" — returns 0 on success */
static int url_split(const char *url, char *host, size_t hostsz, int *port, char *path, size_t pathsz) {
    if (strncmp(url, "http://", 7)) return -1;
    const char *h = url + 7;
    const char *slash = strchr(h, '/');
    const char *hostend = slash ? slash : h + strlen(h);
    const char *colon = memchr(h, ':', (size_t)(hostend - h));
    size_t hl = (size_t)((colon ? colon : hostend) - h);
    if (hl == 0 || hl >= hostsz) return -1;
    memcpy(host, h, hl);
    host[hl] = 0;
    *port = colon ? atoi(colon + 1) : 80;
    snprintf(path, pathsz, "%s", slash ? slash : "/");
    return 0;
}

static int tcp_connect_host(const char *host, int port, int rcv_ms) {
    struct addrinfo hints, *res = NULL;
    char portstr[16];
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_INET;
    hints.ai_socktype = SOCK_STREAM;
    snprintf(portstr, sizeof(portstr), "%d", port);
    if (getaddrinfo(host, portstr, &hints, &res) != 0 || !res) return -1;
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) { freeaddrinfo(res); return -1; }
    {
        /* THE RECEIVE WINDOW THIS CONSOLE PULLS A GAME THROUGH. The 2 MB set on the listening
           socket covers everything the console is ASKED for; it does not reach here, because this
           is a socket we create ourselves to go and fetch something - and fetching is exactly what
           an install does. Set before connect(), so the window is negotiated with it.
           TCP_NODELAY too: this sends a short request and then does nothing but read. */
        int bufsz = 2 << 20;
        setsockopt(s, SOL_SOCKET, SO_RCVBUF, &bufsz, sizeof(bufsz));
        setsockopt(s, SOL_SOCKET, SO_SNDBUF, &bufsz, sizeof(bufsz));
        int one = 1;
        setsockopt(s, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    }
    struct timeval tv;
    tv.tv_sec = rcv_ms / 1000; tv.tv_usec = (rcv_ms % 1000) * 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    /* This one runs on the download worker, not the accept loop, so it was never able to take the
       console down - but an unreachable file host still pinned that worker for 75 seconds and the
       queue showed nothing at all while it did. */
    if (connect_deadline(s, res->ai_addr, res->ai_addrlen, CONNECT_DL_MS) != 0) {
        close(s); freeaddrinfo(res); return -1;
    }
    freeaddrinfo(res);
    return s;
}

/* Stream url -> dest. Resumes with Range when dest already holds bytes, so a
   dropped LAN connection doesn't restart a 7 GB transfer. 0 on success. */
/* TLS lives a long way below this, next to the rest of the sceHttp code, but the dispatch
   belongs at the one door every download already comes through. */
static int https_download(const char *url, const char *dest, char *err, size_t errsz);

static int http_download(const char *url, const char *dest, char *err, size_t errsz) {
    char host[256], path[1100];
    int port = 80;
    /* SAME CONTRACT, DIFFERENT TRANSPORT. https_download writes the same g_job fields, honours
       the same cancel flag and returns the same codes, so every caller - dl_worker, the install
       half, /api/engine/fetch - works over TLS without knowing about it. */
    if (!strncmp(url, "https://", 8)) return https_download(url, dest, err, errsz);
    if (url_split(url, host, sizeof(host), &port, path, sizeof(path)) != 0) {
        snprintf(err, errsz, "only http:// and https:// URLs are supported");
        return -1;
    }

    /* Ask how big the remote file is BEFORE deciding to resume. Blindly sending a Range
       for a file we already hold in full asks for bytes past EOF and earns an HTTP 416,
       which looks like a download failure when the truth is "already downloaded". */
    long long remote = -1;
    {
        int hs = tcp_connect_host(host, port, 15000);
        if (hs >= 0) {
            char hq[1400];
            int hn = snprintf(hq, sizeof(hq),
                              "HEAD %s HTTP/1.1\r\nHost: %s\r\nUser-Agent: PKG-MUTANT-SHOP\r\n"
                              "Connection: close\r\n\r\n", path, host);
            write_all(hs, hq, (size_t)hn);
            char hb[2048];
            size_t hl2 = 0;
            for (;;) {
                ssize_t r = read(hs, hb + hl2, sizeof(hb) - 1 - hl2);
                if (r <= 0) break;
                hl2 += (size_t)r;
                if (hl2 >= sizeof(hb) - 1) break;
            }
            close(hs);
            hb[hl2] = 0;
            const char *lc = strcasestr(hb, "\r\ncontent-length:");
            if (lc) remote = atoll(lc + 17);
        }
    }

    long long have = 0;
    struct stat st;
    if (stat(dest, &st) == 0 && st.st_size > 0) have = (long long)st.st_size;

    if (remote > 0 && have >= remote) {
        /* Already have every byte — don't re-pull several GB just to install it. */
        pthread_mutex_lock(&g_job_mtx);
        g_job.total = remote;
        g_job.done  = remote;
        pthread_mutex_unlock(&g_job_mtx);
        if (have > remote) {                 /* corrupt/oversized leftover — start clean */
            unlink(dest);
        } else {
            return 0;
        }
        have = 0;
    }

    int s = tcp_connect_host(host, port, 30000);
    if (s < 0) { snprintf(err, errsz, "cannot reach %s:%d", host, port); return -1; }

    char req[1500];
    int n;
    if (have > 0)
        n = snprintf(req, sizeof(req),
                     "GET %s HTTP/1.1\r\nHost: %s\r\nRange: bytes=%lld-\r\n"
                     "User-Agent: PKG-MUTANT-SHOP\r\nConnection: close\r\n\r\n", path, host, have);
    else
        n = snprintf(req, sizeof(req),
                     "GET %s HTTP/1.1\r\nHost: %s\r\n"
                     "User-Agent: PKG-MUTANT-SHOP\r\nConnection: close\r\n\r\n", path, host);
    write_all(s, req, (size_t)n);

    /* read headers (bounded) */
    char hdr[4096];
    size_t hl = 0;
    char *body = NULL;
    while (hl < sizeof(hdr) - 1) {
        ssize_t r = read(s, hdr + hl, sizeof(hdr) - 1 - hl);
        if (r <= 0) break;
        hl += (size_t)r;
        hdr[hl] = 0;
        if ((body = strstr(hdr, "\r\n\r\n")) != NULL) { body += 4; break; }
    }
    if (!body) { close(s); snprintf(err, errsz, "no HTTP response headers"); return -1; }

    int status = 0;
    if (!strncmp(hdr, "HTTP/1.", 7)) status = atoi(hdr + 9);
    if (status == 416 && have > 0) {
        close(s);                            /* nothing left to fetch — we already hold it all */
        return 0;
    }
    if (status != 200 && status != 206) {
        close(s);
        snprintf(err, errsz, "server returned HTTP %d", status);
        return -1;
    }
    if (have > 0 && status == 200) have = 0;   /* server ignored Range → start over */

    long long clen = -1;
    const char *cl = strcasestr(hdr, "\r\ncontent-length:");
    if (cl) clen = atoll(cl + 17);

    pthread_mutex_lock(&g_job_mtx);
    g_job.done  = have;
    g_job.total = (clen >= 0) ? have + clen : 0;
    pthread_mutex_unlock(&g_job_mtx);

    int fd = open(dest, O_WRONLY | O_CREAT | (have > 0 ? O_APPEND : O_TRUNC), 0666);
    if (fd < 0) { close(s); snprintf(err, errsz, "cannot write %s", dest); return -1; }

    /* whatever arrived alongside the headers */
    size_t pre = hl - (size_t)(body - hdr);
    if (pre > 0) { write_all(fd, body, pre); have += (long long)pre; }

    char *buf = malloc(DL_BUF);
    if (!buf) { close(fd); close(s); snprintf(err, errsz, "out of memory"); return -1; }
    int rc = 0;
    for (;;) {
        pthread_mutex_lock(&g_job_mtx);
        int cancelled = g_job.cancel;
        g_job.done = have;
        pthread_mutex_unlock(&g_job_mtx);
        if (cancelled) { rc = -2; snprintf(err, errsz, "canceled"); break; }

        ssize_t r = read(s, buf, DL_BUF);
        if (r == 0) break;                       /* clean EOF */
        if (r < 0) { rc = -1; snprintf(err, errsz, "connection dropped at %lld bytes", have); break; }
        if (write_all_checked(fd, buf, (size_t)r) != r) {
            /* Do NOT keep counting. Advancing `have` past bytes that were never stored is exactly
               how a disk-full transfer used to finish "complete" and install a truncated PKG. */
            rc = -1;
            snprintf(err, errsz, "could not write to storage at %lld bytes (disk full?)", have);
            break;
        }
        have += r;
    }
    free(buf);
    close(fd);
    close(s);
    if (rc != 0) return rc;

    pthread_mutex_lock(&g_job_mtx);
    long long want = g_job.total;
    g_job.done = have;
    pthread_mutex_unlock(&g_job_mtx);
    if (want > 0 && have < want) {
        snprintf(err, errsz, "short transfer: got %lld of %lld bytes", have, want);
        return -1;
    }
    return 0;
}

/* PKG content id lives at header offset 0x40, 36 printable chars. Purely informational
   for us, but it is what the UI shows and what proves we got a real package. */
static int pkg_content_id(const char *path, char *out, size_t outsz) {
    if (outsz < 37) return -1;
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    unsigned char h[0x80];
    ssize_t n = read(fd, h, sizeof(h));
    close(fd);
    if (n < (ssize_t)sizeof(h) || h[0] != 0x7F) return -1;
    size_t j = 0;
    for (int i = 0; i < 36; i++) {
        unsigned char c = h[0x40 + i];
        if (c == 0) break;
        if (c < 0x20 || c > 0x7E) return -1;
        out[j++] = (char)c;
    }
    out[j] = 0;
    return out[0] ? 0 : -1;
}

static int install_pkg_local(const char *path, char *cid_out, size_t cid_sz);
static int install_full(const char *uri, int cred_pid, char *cid_out, size_t cid_sz, int *used_pid);
static int find_pid_by_authid(uint64_t want, int maxpid);
/* On FW 12.70 the installer-capable authids are 0x48…, not the 0x38… PS4-era value:
   verified by scanning every live pid — 0x3800000000000010 does not exist here. */
#define AUTHID_SHELLCORE  0x4800000000000010ULL

/* Background worker: download → install → report. Runs detached so the HTTP
   request that started it returns immediately and the UI can poll progress. */
/* All of these live further down the file; the download worker needs them here. */
static void pm_stem(const char *name, char *out, size_t outsz);
static int sha256_file(const char *path, char *hex, size_t hexsz);
static void dl_drop_same_stem(const char *final_path);
static void dl_take_note(const char *dir, const char *name, const char *tag);

static void *dl_worker(void *arg) {
    (void)arg;
    char err[220] = {0};
    job_set(JOB_DOWNLOAD, "Downloading");

    int rc = http_download(g_job.url, g_job.path, err, sizeof(err));
    if (rc != 0) {
        pthread_mutex_lock(&g_job_mtx);
        g_job.state = JOB_ERROR;
        snprintf(g_job.msg, sizeof(g_job.msg), "%s", err[0] ? err : "download failed");
        pthread_mutex_unlock(&g_job_mtx);
        return NULL;
    }

    /* FETCH-ONLY lane: the file is the deliverable. Put it under its real name and stop - there
       is nothing to install, and calling the installer on a .ffpfsc container would be wrong in
       every direction. */
    if (g_job.fetch_only) {
        char from[640], to[640], want[72], purpose[16], rtag[120];
        int clean;
        pthread_mutex_lock(&g_job_mtx);
        snprintf(from, sizeof(from), "%s", g_job.path);
        snprintf(to, sizeof(to), "%s", g_job.final);
        snprintf(want, sizeof(want), "%s", g_job.digest);
        snprintf(purpose, sizeof(purpose), "%s", g_job.purpose);
        snprintf(rtag, sizeof(rtag), "%s", g_job.tag);
        clean = g_job.stem_clean;
        pthread_mutex_unlock(&g_job_mtx);
        /* IS IT THE FILE WE WERE PROMISED? Checked while it is still the .part, so a file that
           is not what the release described never reaches the name something would run. */
        if (want[0]) {
            char got[72] = {0};
            job_set(JOB_DOWNLOAD, "Checking the download");
            if (sha256_file(from, got, sizeof(got)) != 0 || strcasecmp(got, want)) {
                ilog("fetch: %s does not match the release's sha256", to);
                unlink(from);
                pthread_mutex_lock(&g_job_mtx);
                g_job.state = JOB_ERROR;
                snprintf(g_job.msg, sizeof(g_job.msg),
                         "The download does not match what the project published - it was not "
                         "kept.");
                pthread_mutex_unlock(&g_job_mtx);
                return NULL;
            }
        }
        if (clean) dl_drop_same_stem(to);
        unlink(to);                                   /* replacing an older copy is legitimate */
        if (rename(from, to) != 0) {
            ilog("fetch: could not put %s in place (errno %d)", to, errno);
            pthread_mutex_lock(&g_job_mtx);
            g_job.state = JOB_ERROR;
            snprintf(g_job.msg, sizeof(g_job.msg),
                     "Downloaded, but the file could not be put in place on that drive");
            pthread_mutex_unlock(&g_job_mtx);
            return NULL;
        }
        {   /* WRITE DOWN WHAT WAS TAKEN, now that the file is in place. The panel reads this
               instead of the catalogue's version, which is why an update taken on the console
               stops being offered - and it is the proof that this lane, not some PC, put the
               file here. */
            char ndir[640], nbase[300];
            snprintf(ndir, sizeof(ndir), "%s", to);
            char *nsl = strrchr(ndir, '/');
            if (nsl) {
                *nsl = 0;
                snprintf(nbase, sizeof(nbase), "%s", nsl + 1);
                dl_take_note(ndir, nbase, rtag);
            }
        }
        ilog("fetch: delivered %s", to);
        int is_backup = strcmp(purpose, "file") != 0;
        pthread_mutex_lock(&g_job_mtx);
        g_job.state = JOB_DONE;
        snprintf(g_job.path, sizeof(g_job.path), "%s", to);
        snprintf(g_job.msg, sizeof(g_job.msg), "%s",
                 is_backup ? "Downloaded - the backup service will mount it" : "Downloaded");
        pthread_mutex_unlock(&g_job_mtx);
        /* Say it on the television, because this lane produces no PS5 notification of its own -
           the console never sees an install, only a file appearing. And say the RIGHT thing: this
           route's only caller used to be the backup lane, so a downloaded payload was announced
           as a game the backup service was about to mount. */
        if (is_backup)
            notifyf("%s is on your console\nThe backup service is mounting it now",
                    g_job.name[0] ? g_job.name : "Your game");
        else
            notifyf("%s is on your console", g_job.name[0] ? g_job.name : "Your download");
        return NULL;
    }

    char cid[64] = {0};
    pkg_content_id(g_job.path, cid, sizeof(cid));

    job_set(JOB_INSTALL, "Installing");
    char fixed[700];
    rewrite_for_install(g_job.path, fixed, sizeof(fixed));
    char icid[64] = {0};

    /* ONE call, in-process, and this half of dl_worker is a DIAGNOSTIC now (install-url sits
       behind allow-diagnostics). The "ladder" this comment used to describe - InstallByPackage,
       then fall back to AppInstallPkg - no longer exists here and must not come back: the
       fallback is what registered games with no data behind them (see below). In-process
       InstallByPackage answers 0x80B2116F for a base game on this firmware; the product's real
       lane is the SPAWNED installer (/api/engine/install-spawn, spawn_install_wait). What is left
       here: make the call, then go and CHECK - the title must appear in the console's own title
       list AND have its app.pkg, because rc alone is not proof. */
    int used = 0;
    const char *via = "InstallByPackage";
    int irc = install_full(fixed, 0, icid, sizeof(icid), &used);
    /* NO AppInstallPkg FALLBACK FOR A GAME. It returns 0 and registers a title that has no game
       data behind it: the tile appears, the entry looks installed, and launching it takes the
       console down hard enough to need the jailbreak re-run. That was tested here and it cost a
       console. sceAppInstUtilAppInstallPkg is only safe for our own deeplink tile, which has no
       data to install in the first place. A real game install has to go through
       InstallByPackage / BGFT - that is also the only path that produces the PS5's own
       "Downloading / Installing / Ready to play" notifications with the game's artwork. */

    /* Did a title really appear? Read it back rather than trusting the return code. */
    char want_tid[16] = {0};
    {
        const char *src = icid[0] ? icid : cid;
        const char *d = strchr(src, '-');
        if (d) {
            size_t n = 0;
            for (const char *p = d + 1; *p && *p != '_' && n < sizeof(want_tid) - 1; p++)
                want_tid[n++] = *p;
            want_tid[n] = 0;
        }
    }
    /* [audit 29] A TWENTY-SECOND STOPWATCH WAS BEING TURNED INTO A VERDICT, twice over.
       
       First, twenty seconds is not long enough. A large title can take considerably longer than
       that to register on a busy console, and the old loop then declared JOB_ERROR - "never
       appeared in the console's title list" - about an install that was proceeding perfectly well.
       Same mistake the PC-side confirm loop made until [audit 11]; the cure is the same. Wait far
       longer, and only give up when nothing has changed for a while rather than at a fixed count.
       
       Second, appearing in the title list is NOT proof the game is there. A metadata-only
       registration puts a title in the list with no data behind it - that is the broken tile that
       fails with "Cannot start the game", and this project has a five-file rule precisely because
       of it. So the title list is the trigger to LOOK, and installed_app_pkg-style evidence is
       what actually decides. */
    int registered = 0, have_data = 0;
    if (want_tid[0]) {
        title_t *rows = (title_t *)calloc(MAX_TITLES, sizeof(title_t));
        for (int tries = 0; tries < 300 && !have_data; tries++) {
            int tn = rows ? read_console_titles(rows, MAX_TITLES) : -1;
            for (int i = 0; i < tn; i++)
                if (!strcmp(rows[i].tid, want_tid)) { registered = 1; break; }
            if (registered) {
                /* The game's own app.pkg is the proof. Check every root it can live on. */
                static const char *roots[] = { "/user/app", "/mnt/ext0/user/app",
                                               "/mnt/ext1/user/app", "/mnt/ext2/user/app" };
                for (unsigned r = 0; r < sizeof(roots) / sizeof(roots[0]) && !have_data; r++) {
                    char pk[700];
                    struct stat pst;
                    snprintf(pk, sizeof(pk), "%s/%s/app.pkg", roots[r], want_tid);
                    if (stat(pk, &pst) == 0 && pst.st_size > 0) have_data = 1;
                }
            }
            if (!have_data) sleep(1);
        }
        free(rows);
    }

    pthread_mutex_lock(&g_job_mtx);
    g_job.rc = irc;
    snprintf(g_job.content_id, sizeof(g_job.content_id), "%s", icid[0] ? icid : cid);
    if (irc == 0 && have_data) {
        g_job.state = JOB_DONE;
        snprintf(g_job.msg, sizeof(g_job.msg), "Installed - the game's files are on the console");
    } else if (irc == 0 && registered) {
        /* Registered but no data behind it. Saying "installed" here is what puts a tile on the
           dashboard that fails to launch, so it is reported as the specific thing it is. */
        g_job.state = JOB_ERROR;
        snprintf(g_job.msg, sizeof(g_job.msg),
                 "%s is listed on the console but its game files are not there - do not launch it. "
                 "Delete it on the PS5 and install again. The package is kept at %s",
                 want_tid[0] ? want_tid : "The title", g_job.path);
    } else if (irc == 0) {
        g_job.state = JOB_ERROR;
        snprintf(g_job.msg, sizeof(g_job.msg),
                 "The console accepted this package but has not finished installing it. Check the "
                 "PS5's own download notification - nothing here was changed. The package is kept "
                 "at %s", g_job.path);
    } else {
        g_job.state = JOB_ERROR;
        snprintf(g_job.msg, sizeof(g_job.msg),
                 "downloaded OK, but the console refused the install (0x%08X) - "
                 "the PKG is kept at %s", (unsigned)irc, g_job.path);
    }
    pthread_mutex_unlock(&g_job_mtx);
    return NULL;
}

/* read one ?key=value out of the raw request path, FULL percent-decoding.
   A partial decoder is a trap here: a URL passed as ?uri=... arrives with %3A for
   the scheme colon, and handing "http%3A//host/x.pkg" to the installer fails with
   an unhelpful 0x80B21106. Decode every %XX, and '+' as space. */
static int hexval(char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    if (c >= 'A' && c <= 'F') return c - 'A' + 10;
    return -1;
}
static int qparam(const char *raw, const char *key, char *out, size_t outsz) {
    char pat[64];
    snprintf(pat, sizeof(pat), "%s=", key);
    const char *p = strstr(raw, pat);
    if (!p) return 0;
    p += strlen(pat);
    size_t j = 0;
    while (*p && *p != '&' && j < outsz - 1) {
        if (p[0] == '%' && hexval(p[1]) >= 0 && hexval(p[2]) >= 0) {
            out[j++] = (char)((hexval(p[1]) << 4) | hexval(p[2]));
            p += 3;
        } else if (p[0] == '+') { out[j++] = ' '; p++; }
        else out[j++] = *p++;
    }
    out[j] = 0;
    return 1;
}





/* sanitize a request path into a real file under WEB_ROOT (no traversal) */
/* THE UI, STRAIGHT OUT OF THIS ELF. WEB_FILES[] is in this process's memory for its whole life -
   it is what extract_web() writes to disk at boot - and serve_static only ever read the copy on
   /data. If that read failed for any reason (a full or busy filesystem, a half-written extract, a
   file somebody removed) the console answered 404 for its own page while the port stayed bound and
   every API route kept working. From the browser that is indistinguishable from the stall this
   release is about: a page that will not load, with the bare URL left in the title bar.
   Returns 1 when it served something. Disk still wins, so a config.js the owner edited is still
   theirs; this is only ever the answer to "the disk would not give it to us". */
static int serve_embedded(int fd, const char *rel) {
    if (!rel || !*rel) return 0;
    for (int i = 0; i < WEB_FILES_COUNT; i++) {
        if (strcmp(WEB_FILES[i].path, rel)) continue;
        const char *ct = ctype_for(rel);
        char hdr[320];
        int hl = snprintf(hdr, sizeof(hdr),
                          "HTTP/1.1 200 OK\r\nContent-Type: %s\r\nContent-Length: %u\r\n"
                          "Cache-Control: no-cache, must-revalidate\r\n"
                          "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n",
                          ct ? ct : "application/octet-stream", WEB_FILES[i].len);
        if (hl <= 0 || (size_t)hl >= sizeof(hdr)) return 0;
        write_all(fd, hdr, (size_t)hl);
        write_all(fd, (const char *)WEB_FILES[i].data, (size_t)WEB_FILES[i].len);
        ilog("static: served %s from the copy inside this ELF - /data would not give it up", rel);
        return 1;
    }
    return 0;
}

static void serve_static(int fd, const char *path, const char *req) {
    if (strstr(path, "..")) { send_status(fd, "403 Forbidden", "text/plain", "no"); return; }
    char full[600];
    const char *rel = (!strcmp(path, "/") || path[0] == 0) ? "index.html"
                                                           : (path[0] == '/' ? path + 1 : path);
    if (!strcmp(path, "/") || path[0] == 0)
        snprintf(full, sizeof(full), "%s/index.html", WEB_ROOT);
    else
        snprintf(full, sizeof(full), "%s%s", WEB_ROOT, path);
    if (send_file_req(fd, full, req) == 0) return;
    if (serve_embedded(fd, rel)) return;
    send_status(fd, "404 Not Found", "text/plain", "not found");
}

/* PC companion URL, pushed here by the companion (server.py) whenever it's running + can reach the PS5.
   The shop UI reads /api/companion and upgrades to it → PS5 auto-finds the PC, both directions. */
static char g_pc_url[128] = {0};
/* install_pkg() / install_pkg_full() used to be declared here "defined below". Neither was ever
   defined or called; the tile goes through install_pkg_local() and games through the spawned
   installer. Gone, so nobody links against an API that does not exist. */
/* InstallByPackage from THIS process (the real data install) */
static int install_by_package_inproc(const char *uri, char *cid_out, size_t cid_sz);
static int install_by_package_inproc_ex(const char *uri, char *cid_out, size_t cid_sz,
                                        int announce_bigapp,        /* A/B: skip the big-app announce */
                                        unsigned long long authid,  /* A/B: call as another authid */
                                        int ctype, int cplat);      /* A/B: SceAppInstallPkgInfo fields */
static int port_busy(int port);
static int ftp_live_port(void);   /* 2121 (Arsenal ftpsrv) or 1337 (etaHEN), 0 if neither */
static int mem_read(pid_t pid, intptr_t addr, void *buf, size_t len);
static int mem_write(pid_t pid, intptr_t addr, const void *buf, size_t len);
static int hex2bytes(const char *hex, unsigned char *out, size_t cap);
static int running_game(char *title, size_t tsz, pid_t *out_pid, intptr_t *out_base);
static const char *cheat_mod_state_blk(const char *json, const char *blk, const char *end,
                                       pid_t pid, intptr_t base, int non_json);
static size_t patch_unescape(const char *in, size_t len, char *out, size_t outsz);
/* Our own library. The ELF creates it and migrates whatever it finds in the old CheatRunner
   location, so the app owns everything it needs under one directory. */
#define SHOP_DATA_DIR     "/data/pkg-mutant-shop"
/* THUMB_CACHE_DIR is written out in full further up, because it is needed before this line. If
   the shop's data directory ever moves, this refuses to build rather than leaving the thumbnail
   cache pointing at the old one. */
_Static_assert(sizeof(THUMB_CACHE_DIR) == sizeof(SHOP_DATA_DIR "/thumbs")
               && __builtin_strcmp(THUMB_CACHE_DIR, SHOP_DATA_DIR "/thumbs") == 0,
               "THUMB_CACHE_DIR must stay under SHOP_DATA_DIR");
#define PB_DIR      "/data/pkg-mutant-shop/payloads"
/* SEEDED ONCE, KEPT FOR EVER. The homebrew packages are 264 MB and cannot ride inside an ELF that
   Payload Manager loads into RAM, so the PC copies them here the first time the two meet and the
   console installs them from here afterwards with no PC at all - pkgfile_path_allowed() already
   accepts /data/..., so the ordinary local install lane serves them to the installer over
   loopback with nothing new to get right. */
#define HB_DIR      "/data/pkg-mutant-shop/homebrews"
#define CHEAT_ROOT        SHOP_DATA_DIR "/cheats"
#define CHEAT_JSON_DIR    CHEAT_ROOT "/json"
#define CHEAT_SHN_DIR     CHEAT_ROOT "/shn"
#define CHEAT_MC4_DIR     CHEAT_ROOT "/mc4"
#define CHEAT_PATCH_DIR   CHEAT_ROOT "/patches"
#define LEGACY_CHEAT_ROOT "/data/cheatrunner/cheats"
#define LEGACY_PATCH_DIR  "/data/cheatrunner/patches"
/* Sized from the real library, not guessed: across a 400-file sample the largest file held
   74 mods, the largest mod 41 entries, and the largest single patch 520 bytes.
   CHEAT_MAX_BYTES was 1024. Measured over the WHOLE shipped library that quietly dropped 5 JSON
   entries and 7 .shn cheatlines (1-4 KB code caves, e.g. CUSA01875_01.01 "Max Items"): the rest
   of the mod was written and the toast said ON with the hook missing a piece. 4096 holds every
   entry except four ~470 KB caves nobody should write blind; those, and anything malformed, are
   now COUNTED (parse_mod_entries_ex) and refuse the whole mod instead of applying part of it. */
#define CHEAT_MAX_MODS    256
#define CHEAT_MAX_ENTRIES 128
#define CHEAT_MAX_BYTES   4096
typedef struct {
    unsigned long long offset;
    unsigned char on[CHEAT_MAX_BYTES];  int on_len;
    unsigned char off[CHEAT_MAX_BYTES]; int off_len;
    int absolute;              /* <Absolute> in .shn/.mc4 — offset is not image-relative */
    /* THE MODULE THE CHEAT MEANT. Talixme's trainer records the index of a loaded module in
       <Section>, and 306 entries in the library we ship carry a non-zero one. This engine resolves
       ONE base - the main executable - so for those entries base + offset is not where the cheat
       points, and the gate refuses them. Kept only so the refusal can say that, instead of blaming
       the game's version for something that is a property of the cheat file. */
    int section;
} cheat_entry_t;
/* Canonical x86-64 user range: outside this we risk kernel space or PS5 MMIO. */
#define ADDR_OK(a) ((intptr_t)(a) >= 0x1000L && (intptr_t)(a) <= (intptr_t)0x7FFFFFFFFFFFL)
/* An entry array is now ~1 MB, far too much for a thread stack — always heap-allocate. */
#define CHEAT_ENTS_BYTES ((size_t)CHEAT_MAX_ENTRIES * sizeof(cheat_entry_t))
static int parse_mod_entries(const char *blk, const char *blk_end, cheat_entry_t *out, int max);
static int parse_mod_entries_ex(const char *blk, const char *blk_end, cheat_entry_t *out, int max,
                                int *dropped_out);
static const char *next_mod_block(const char *from, const char **end);
static const char *mods_array_start(const char *json);
static int cheat_apply_mod_doc(const char *json, int non_json, int index, int want_on, pid_t pid,
                               intptr_t base, int force, int check_only, char *detail, size_t dsz);
static int cheat_apply_blk(const char *json, int non_json, const char *blk, const char *end,
                           int index, int want_on, pid_t pid, intptr_t base, int force,
                           int check_only, char *detail, size_t dsz);
/* The signature search: the routes that start and read it sit ~2,000 lines above the engine. */
typedef struct pms_sig pms_sig_t;
static int sig_start(const char *pattern, pid_t pid, intptr_t base, long long from, long long to);
static void sig_cancel(void);
static void sig_status_json(char *out, size_t osz);
static const char *cheat_master_span(const char *json, const char **end);
static int cheat_master_info(const char *json, int *removable);
static int cheat_master_off(const char *json, pid_t pid, intptr_t base, int non_json,
                            char *why, size_t wsz);
/* Declared here because the routes that answer with it sit above its definition. */
static void cheat_rc_message(int rc, const char *detail, int want_on, char *out, size_t osz);
static char *slurp(const char *path, long *out_len);
static const char *json_str_after(const char *p, const char *key, char *out, size_t outsz);
static const char *json_str_after_lim(const char *p, const char *end, const char *key,
                                      char *out, size_t outsz);
static const char *find_mod_block(const char *json, int index, const char **end);
static int cheat_apply_mod(const char *file, int index, int want_on, pid_t pid, intptr_t base,
                           int force, int check_only, char *detail, size_t dsz);
static int install_title_dir(const char *tid, int do_uninstall_first, int *rc_uninstall, int *rc_all);
#define TILE_TID       "PKGM00001"
#define TILE_APP_DIR   "/user/app/" TILE_TID
#define TILE_META_DIR  "/user/appmeta/" TILE_TID
#define TILE_ICON_SRC  WEB_ROOT "/assets/icon0.png"
static int tile_install(int force, char *detail, size_t dsz);   /* dashboard tile (PKGM00001) */
static int tile_is_registered(void);

/* ================ GAME PATCHES — GoldHEN / PS-Game-Patch XML ==================
   One file per title id: <patches>/<TITLEID>.xml. Shape (measured across the real
   376-file library, nothing assumed):

     <Patch>
       <TitleID><ID>PPSA01339</ID>…</TitleID>          up to 25 ids share one patch
       <Metadata Title= Name= Note= Author= PatchVer= AppVer= AppElf= [ImageBase=]>
         <PatchList>
           <Line Type="bytes" Address="0x0059ea79" Value="e9c2f41a01"/>
   Each <Metadata> is ONE user-visible patch, and a file carries up to 59 of them —
   normally the same patch rebuilt for different AppVer values. AppVer is what makes a
   patch safe or catastrophic: it is compiled against exact code addresses.

   ADDRESS: `Address` ALREADY INCLUDES the load base. The offset is
   `Address - (ImageBase, or 0x400000 when the attribute is absent)`, and the write lands at
   runtime_base + offset. 0x400000 is the PS4/PS5 no-ASLR load address, and every reference
   implementation does exactly this: ps-patch-system `resolve_addr()` = mapbase + (addr - base)
   with `NO_ASLR_ADDR = 0x00400000`; etaHEN `(ImageBaseAddr == 0 ? NO_ASLR_ADDR_PS4 : ...)`;
   GoldHEN `addr_real = g_module_base + (addr_real - NO_ASLR_ADDR)`; shadPS4 `- 0x400000`.
   The library agrees: of 19,167 addresses in files with no ImageBase the smallest is 0x401c22
   and NOT ONE is below 0x400000 — which is what base-included addresses look like. Treating
   them as image-relative would put every PS4-style patch 0x400000 bytes too high. */
#define PATCH_NO_ASLR 0x400000ULL
#define PATCH_MAX_ITEMS   128    /* <Metadata> per file  — real-library max is 59     */
#define PATCH_MAX_LINES  1024    /* <Line> per patch      — real-library max is 569    */
/* Bytes one line writes. Measured by ENCODING every line in the library, not by the length of
   the Value text: a utf8 line in CUSA13233 expands to 970 bytes, so the obvious "max hex/2 =
   122" reading of the same corpus is wrong by 8x. The line array is heap-only (~1 MB). */
#define PATCH_MAX_VAL    1024
typedef struct {
    unsigned long long off;                 /* image-relative offset                  */
    unsigned char      val[PATCH_MAX_VAL];
    int                len;
    int                unsupported;         /* mask/mask_jump32: needs a signature scan */
} patch_line_t;
#define PATCH_LINES_BYTES ((size_t)PATCH_MAX_LINES * sizeof(patch_line_t))
static int  patch_file_for(const char *tid, char *out, size_t outsz);
static int  patch_count(const char *doc);
static const char *patch_block(const char *doc, int index, const char **end);
static int  patch_encode_value(const char *type, size_t tlen, const char *val, size_t vlen,
                               unsigned char *out, size_t outsz);
static int  patch_parse_lines(const char *blk, const char *end, patch_line_t *out, int max,
                              int *unsupported_out);
static int appinst_once(void);      /* resolve+init libSceAppInstUtil exactly once */
static int  patch_apply(const char *tid, int index, const char *iver, pid_t pid, intptr_t base,
                        int force, int dry, char *detail, size_t dsz);
static int  patch_revert(const char *tid, int index, const char *iver, pid_t pid, intptr_t base,
                         char *detail, size_t dsz);
static size_t patches_json(const char *tid, const char *iver, char *out, size_t outsz);
static void patch_action_json(const char *tid, int index, int force, int dry, int is_revert,
                              char *out, size_t outsz);
static int (*g_ai_titledir)(const char *, const char *, void *);
static int (*g_ai_installall)(void *);
static int (*g_ai_uninstall)(const char *);
static char g_preload_log[320];
static char g_pb_log[320];
static char g_tile_log[320];   /* what the last dashboard-tile attempt reported */
static int g_ai_init_rc = -1;   /* whatever sceAppInstUtilInitialize actually returned */

/* ============================ AES-256-CBC (decrypt only) ================== */

static uint8_t aes_sbox_tbl[256], aes_rsbox_tbl[256];
static int aes_tables_ready = 0;

/* Generate the S-box rather than carrying a literal table: same result, far less
   source to get wrong, and the inverse falls out for free. */
static void aes_build_tables(void) {
    if (aes_tables_ready) return;
    uint8_t p = 1, q = 1;
    do {
        p = (uint8_t)(p ^ (uint8_t)(p << 1) ^ (uint8_t)((p & 0x80) ? 0x1B : 0));
        q ^= (uint8_t)(q << 1);
        q ^= (uint8_t)(q << 2);
        q ^= (uint8_t)(q << 4);
        if (q & 0x80) q ^= 0x09;
        uint8_t x = (uint8_t)(q ^ (uint8_t)((q << 1) | (q >> 7))
                                ^ (uint8_t)((q << 2) | (q >> 6))
                                ^ (uint8_t)((q << 3) | (q >> 5))
                                ^ (uint8_t)((q << 4) | (q >> 4)) ^ 0x63);
        aes_sbox_tbl[p] = x;
    } while (p != 1);
    aes_sbox_tbl[0] = 0x63;
    for (int i = 0; i < 256; i++) aes_rsbox_tbl[aes_sbox_tbl[i]] = (uint8_t)i;
    aes_tables_ready = 1;
}

static uint8_t aes_gmul(uint8_t a, uint8_t b) {
    uint8_t r = 0;
    for (int i = 0; i < 8; i++) {
        if (b & 1) r ^= a;
        uint8_t hi = (uint8_t)(a & 0x80);
        a = (uint8_t)(a << 1);
        if (hi) a ^= 0x1B;
        b = (uint8_t)(b >> 1);
    }
    return r;
}

/* AES-256: Nk=8, Nr=14 -> 15 round keys (240 bytes). */
static void aes256_expand(const uint8_t *key, uint8_t *rk) {
    aes_build_tables();
    memcpy(rk, key, 32);
    uint8_t rcon = 1;
    for (int i = 8; i < 60; i++) {
        uint8_t t[4];
        memcpy(t, rk + (i - 1) * 4, 4);
        if (i % 8 == 0) {
            uint8_t tmp = t[0];
            t[0] = (uint8_t)(aes_sbox_tbl[t[1]] ^ rcon);
            t[1] = aes_sbox_tbl[t[2]];
            t[2] = aes_sbox_tbl[t[3]];
            t[3] = aes_sbox_tbl[tmp];
            rcon = aes_gmul(rcon, 2);
        } else if (i % 8 == 4) {
            for (int j = 0; j < 4; j++) t[j] = aes_sbox_tbl[t[j]];
        }
        for (int j = 0; j < 4; j++) rk[i * 4 + j] = (uint8_t)(rk[(i - 8) * 4 + j] ^ t[j]);
    }
}

static void aes_add_round_key(uint8_t *s, const uint8_t *rk, int round) {
    for (int i = 0; i < 16; i++) s[i] ^= rk[round * 16 + i];
}

static void aes_inv_shift_rows(uint8_t *s) {
    uint8_t t;
    t = s[13]; s[13] = s[9]; s[9] = s[5]; s[5] = s[1]; s[1] = t;             /* row1 >>1 */
    t = s[2];  s[2] = s[10]; s[10] = t;  t = s[6]; s[6] = s[14]; s[14] = t;  /* row2 >>2 */
    t = s[3];  s[3] = s[7];  s[7] = s[11]; s[11] = s[15]; s[15] = t;         /* row3 >>3 */
}

static void aes_inv_sub_bytes(uint8_t *s) {
    for (int i = 0; i < 16; i++) s[i] = aes_rsbox_tbl[s[i]];
}

static void aes_inv_mix_columns(uint8_t *s) {
    for (int c = 0; c < 4; c++) {
        uint8_t *p = s + c * 4;
        uint8_t a0 = p[0], a1 = p[1], a2 = p[2], a3 = p[3];
        p[0] = (uint8_t)(aes_gmul(a0,14) ^ aes_gmul(a1,11) ^ aes_gmul(a2,13) ^ aes_gmul(a3, 9));
        p[1] = (uint8_t)(aes_gmul(a0, 9) ^ aes_gmul(a1,14) ^ aes_gmul(a2,11) ^ aes_gmul(a3,13));
        p[2] = (uint8_t)(aes_gmul(a0,13) ^ aes_gmul(a1, 9) ^ aes_gmul(a2,14) ^ aes_gmul(a3,11));
        p[3] = (uint8_t)(aes_gmul(a0,11) ^ aes_gmul(a1,13) ^ aes_gmul(a2, 9) ^ aes_gmul(a3,14));
    }
}

static void aes256_decrypt_block(const uint8_t *rk, uint8_t *b) {
    aes_add_round_key(b, rk, 14);
    for (int round = 13; round >= 1; round--) {
        aes_inv_shift_rows(b);
        aes_inv_sub_bytes(b);
        aes_add_round_key(b, rk, round);
        aes_inv_mix_columns(b);
    }
    aes_inv_shift_rows(b);
    aes_inv_sub_bytes(b);
    aes_add_round_key(b, rk, 0);
}

static void aes256_cbc_decrypt(const uint8_t *key, const uint8_t *iv,
                               uint8_t *buf, size_t len) {
    uint8_t rk[240], prev[16], cur[16];
    aes256_expand(key, rk);
    memcpy(prev, iv, 16);
    for (size_t off = 0; off + 16 <= len; off += 16) {
        memcpy(cur, buf + off, 16);
        aes256_decrypt_block(rk, buf + off);
        for (int i = 0; i < 16; i++) buf[off + i] ^= prev[i];
        memcpy(prev, cur, 16);
    }
}

/* ================================ base64 ================================= */

static int b64val(int c) {
    if (c >= 'A' && c <= 'Z') return c - 'A';
    if (c >= 'a' && c <= 'z') return c - 'a' + 26;
    if (c >= '0' && c <= '9') return c - '0' + 52;
    if (c == '+') return 62;
    if (c == '/') return 63;
    return -1;
}

/* Returns malloc'd bytes, or NULL. Whitespace is skipped; '=' ends the stream. */
static uint8_t *b64_decode(const char *in, size_t in_len, size_t *out_len) {
    uint8_t *out = (uint8_t *)malloc(in_len / 4 * 3 + 4);
    if (!out) return NULL;
    size_t n = 0;
    int acc = 0, bits = 0;
    for (size_t i = 0; i < in_len; i++) {
        int c = (unsigned char)in[i];
        if (c == '=') break;
        int v = b64val(c);
        if (v < 0) continue;                       /* newlines / stray bytes */
        acc = (acc << 6) | v;
        bits += 6;
        if (bits >= 8) {
            bits -= 8;
            out[n++] = (uint8_t)((acc >> bits) & 0xFF);
        }
    }
    *out_len = n;
    return out;
}

/* ============================ growable text buffer ======================= */

typedef struct { char *buf; size_t len, cap; } tbuf_t;

static int tbuf_need(tbuf_t *b, size_t extra) {
    if (b->len + extra + 1 <= b->cap) return 0;
    size_t cap = b->cap ? b->cap : 4096;
    while (cap < b->len + extra + 1) cap *= 2;
    char *p = (char *)realloc(b->buf, cap);
    if (!p) return -1;
    b->buf = p; b->cap = cap;
    return 0;
}
static void tbuf_putn(tbuf_t *b, const char *s, size_t n) {
    if (tbuf_need(b, n)) return;
    memcpy(b->buf + b->len, s, n);
    b->len += n;
    b->buf[b->len] = 0;
}
static void tbuf_puts(tbuf_t *b, const char *s) { tbuf_putn(b, s, strlen(s)); }
static void tbuf_putc(tbuf_t *b, char c) { tbuf_putn(b, &c, 1); }

/* JSON-escape, and drop control characters that would break the document. */
static void tbuf_put_json(tbuf_t *b, const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) {
        unsigned char c = (unsigned char)s[i];
        if (c == '"' || c == '\\') { tbuf_putc(b, '\\'); tbuf_putc(b, (char)c); }
        else if (c == '\n' || c == '\r' || c == '\t') tbuf_putc(b, ' ');
        else if (c < 0x20) continue;
        else tbuf_putc(b, (char)c);
    }
}

/* Hex bytes in these files are dash-separated ("90-90-90-90"). Emit them bare so the
   existing JSON entry parser needs no change. Non-hex characters are dropped. */
static void tbuf_put_hex(tbuf_t *b, const char *s, size_t n) {
    for (size_t i = 0; i < n; i++) {
        char c = s[i];
        if ((c >= '0' && c <= '9') || (c >= 'a' && c <= 'f') || (c >= 'A' && c <= 'F'))
            tbuf_putc(b, c);
    }
}

/* ================================ tiny XML =============================== */

static int isalnum_c(int c) {
    return (c >= '0' && c <= '9') || (c >= 'a' && c <= 'z') || (c >= 'A' && c <= 'Z');
}

/* Value of attr in a start-tag chunk, e.g. Text="Godmode". Not a general XML parser --
   it only has to read the fixed Trainer schema. */
static const char *xml_attr(const char *tag, const char *attr, size_t *len_out) {
    size_t alen = strlen(attr);
    for (const char *p = tag; (p = strstr(p, attr)) != NULL; p += alen) {
        if (p > tag && (isalnum_c(p[-1]) || p[-1] == '_')) continue;   /* substring of another attr */
        const char *q = p + alen;
        while (*q == ' ' || *q == '\t') q++;
        if (*q != '=') continue;
        q++;
        while (*q == ' ' || *q == '\t') q++;
        char quote = *q;
        if (quote != '"' && quote != '\'') continue;
        q++;
        const char *end = strchr(q, quote);
        if (!end) return NULL;
        *len_out = (size_t)(end - q);
        return q;
    }
    return NULL;
}

/* Text of <name>...</name> inside chunk. */
static const char *xml_child(const char *chunk, const char *name, size_t *len_out) {
    char open[64];
    int n = snprintf(open, sizeof(open), "<%s>", name);
    if (n <= 0 || (size_t)n >= sizeof(open)) return NULL;
    const char *s = strstr(chunk, open);
    if (!s) return NULL;
    s += (size_t)n;
    char close[64];
    snprintf(close, sizeof(close), "</%s>", name);
    const char *e = strstr(s, close);
    if (!e) return NULL;
    while (s < e && (*s == ' ' || *s == '\n' || *s == '\r' || *s == '\t')) s++;
    const char *t = e;
    while (t > s && (t[-1] == ' ' || t[-1] == '\n' || t[-1] == '\r' || t[-1] == '\t')) t--;
    *len_out = (size_t)(t - s);
    return s;
}

/* ==================== Trainer XML  ->  our cheat JSON ==================== */

/* Attribute text out of the Trainer XML, entities decoded, into the JSON. The names used to be
   copied raw, so 74 cheats showed '&quot;' and '&amp;' literally on the panel (the UI escapes
   what it is given, as it should). The same decoder the patch Values already use fixes it. */
static void tbuf_put_json_xml(tbuf_t *b, const char *s, size_t n) {
    char tmp[2048];
    size_t l = patch_unescape(s, n, tmp, sizeof(tmp));
    tbuf_put_json(b, tmp, l);
}

/* One <Cheatline> is copied out to be scanned. This was a 4 KB stack chunk and a longer line
   was skipped in silence: 14 .shn and 2 .mc4 cheatlines in the shipped library are longer (a
   10 KB one in CUSA01875_01.01 "Max Items"), so the rest of the mod was written and the toast
   said ON while the hook was missing a piece. Heap now, and generous; a line beyond even this
   is COUNTED as dropped so the mod can say so instead of pretending. */
#define SHN_CHUNK_MAX ((size_t)64 << 10)

/* Returns a malloc'd JSON document in the shape our engine already parses, or NULL. */
static char *shn_xml_to_json(const char *xml, size_t xml_len) {
    if (!xml) return NULL;
    /* UTF-16 IS NOT A PARSE FAILURE THIS CAN HIDE. Four .shn files in the shipped library are
       UTF-16, and every strstr below misses on the NUL bytes - so the result was a well-formed
       document with no cheats in it, which reads to the owner as "this game has nothing" rather
       than "this file cannot be read". NULL makes the caller say the second. */
    if (xml_len >= 2) {
        unsigned char b0 = (unsigned char)xml[0], b1 = (unsigned char)xml[1];
        if ((b0 == 0xFF && b1 == 0xFE) || (b0 == 0xFE && b1 == 0xFF)) return NULL;
        if (b0 == 0x00 || b1 == 0x00) return NULL;      /* UTF-16 with no mark */
    }
    char *chunk = (char *)malloc(SHN_CHUNK_MAX);
    if (!chunk) return NULL;
    tbuf_t o = {0};
    size_t al = 0;
    const char *v;

    const char *trainer = strstr(xml, "<Trainer");
    char hdr[2048];
    hdr[0] = 0;
    if (trainer) {
        const char *close = strchr(trainer, '>');
        if (close) {
            size_t hn = (size_t)(close - trainer);
            if (hn < sizeof(hdr)) { memcpy(hdr, trainer, hn); hdr[hn] = 0; }
        }
    }

    tbuf_puts(&o, "{\"name\":\"");
    if (hdr[0] && ((v = xml_attr(hdr, "Game", &al)) || (v = xml_attr(hdr, "GameName", &al))))
        tbuf_put_json_xml(&o, v, al);
    tbuf_puts(&o, "\",\"id\":\"");
    if (hdr[0] && ((v = xml_attr(hdr, "Cusa", &al)) || (v = xml_attr(hdr, "TitleId", &al))))
        tbuf_put_json(&o, v, al);
    tbuf_puts(&o, "\",\"version\":\"");
    if (hdr[0] && (v = xml_attr(hdr, "Version", &al))) tbuf_put_json(&o, v, al);
    tbuf_puts(&o, "\",\"process\":\"");
    if (hdr[0] && (v = xml_attr(hdr, "Process", &al))) tbuf_put_json(&o, v, al);
    else tbuf_puts(&o, "eboot.bin");
    tbuf_putc(&o, '"');
    tbuf_putc(&o, ',');

    /* ---- <StartUP> -> "master" ---------------------------------------------------------------
     * 126 blocks in 73 Trainer files, named "Master Code 1 (Must Be On)" and meant exactly that: a
     * shared routine and its scratch, which the cheats in the same file are diffs against. It was
     * never looked at, because this loop only ever walked <Cheat>. Emitted into the same key the
     * json files use, so cheat_master_decide reads one shape for both formats - and these carry
     * ValueOff, so a master that came from here can be removed again.
     * Written before "mods" only for tidiness; nothing depends on key order. */
    /* EVERY <StartUP>, NOT THE FIRST. 126 blocks across 73 files, so 36 of them carry more than one -
       "Master Code 1", "Master Code 2" - and the cheats in those files are diffs against all of them.
       Reading one of two installed half a routine, which is the failure the rest of this engine
       refuses by name. They all go into the one "master" object, which is how the engine treats it:
       one list, applied whole or not at all. */
    {
        int su_any = 0, su_first = 1, su_dropped = 0;
        const char *su = xml;
        while ((su = strstr(su, "<StartUP")) != NULL) {
            const char *suc = strchr(su, '>');
            const char *su_end = strstr(su, "</StartUP>");
            if (!suc || !su_end) break;
            if (!su_any) { tbuf_puts(&o, "\"master\":{\"memory\":["); su_any = 1; }
            const char *lc = suc;
            while (lc < su_end && (lc = strstr(lc, "<Cheatline")) != NULL && lc < su_end) {
                /* CLAMPED TO THIS BLOCK. These came from strstr over the whole rest of the document,
                   so a cheatline whose </Cheatline> was missing copied a following <Cheat>'s bytes
                   into the master. A line that does not close inside the block ends it. */
                const char *lclose = strstr(lc, "</Cheatline>");
                const char *lself  = strstr(lc, "/>");
                if (lclose && lclose >= su_end) lclose = NULL;
                if (lself && lself >= su_end) lself = NULL;
                const char *lend;
                if (lclose && (!lself || lclose < lself)) lend = lclose + 12;
                else if (lself) lend = lself + 2;
                else break;
                size_t cl = (size_t)(lend - lc);
                if (cl >= SHN_CHUNK_MAX) { su_dropped++; lc = lend; continue; }
                memcpy(chunk, lc, cl); chunk[cl] = 0;
                size_t ol = 0, onl = 0, offl = 0, scl = 0;
                const char *off  = xml_child(chunk, "Offset",   &ol);
                const char *on   = xml_child(chunk, "ValueOn",  &onl);
                const char *offv = xml_child(chunk, "ValueOff", &offl);
                const char *sect = xml_child(chunk, "Section",  &scl);
                if (off && ol && on && onl) {
                    if (!su_first) tbuf_putc(&o, ',');
                    su_first = 0;
                    tbuf_puts(&o, "{\"offset\":\"");
                    tbuf_put_hex(&o, off, ol);
                    tbuf_puts(&o, "\",\"on\":\"");
                    tbuf_put_hex(&o, on, onl);
                    tbuf_puts(&o, "\",\"off\":\"");
                    if (offv) tbuf_put_hex(&o, offv, offl);
                    tbuf_putc(&o, '"');
                    if (sect && scl) {
                        tbuf_puts(&o, ",\"section\":\"");
                        tbuf_put_json(&o, sect, scl);
                        tbuf_putc(&o, '"');
                    }
                    tbuf_putc(&o, '}');
                }
                lc = lend;
            }
            su = su_end + 10;
        }
        if (su_any) {
            tbuf_puts(&o, "]");
            /* A MASTER MISSING A PIECE IS WORSE THAN A MOD MISSING ONE. The Cheat loop counts an
               oversize line and makes its mod refuse; this silently skipped it. cheat_master_decide
               reads "dropped" and refuses the whole master (MW_PART). */
            if (su_dropped) {
                char dn[40];
                snprintf(dn, sizeof(dn), ",\"dropped\":%d", su_dropped);
                tbuf_puts(&o, dn);
            }
            tbuf_puts(&o, "},");
        }
    }
    tbuf_puts(&o, "\"mods\":[");

    int first = 1;
    const char *cur = xml;
    while ((cur = strstr(cur, "<Cheat ")) != NULL) {
        const char *close = strchr(cur, '>');
        if (!close) break;
        size_t hn = (size_t)(close - cur);
        char ch[2048];
        if (hn >= sizeof(ch)) { cur = close + 1; continue; }
        memcpy(ch, cur, hn); ch[hn] = 0;

        const char *body_end = strstr(close, "</Cheat>");
        if (!body_end) break;

        if (!first) tbuf_putc(&o, ',');
        first = 0;
        tbuf_puts(&o, "{\"name\":\"");
        if ((v = xml_attr(ch, "Text", &al)) || (v = xml_attr(ch, "CheatName", &al)) ||
            (v = xml_attr(ch, "Name", &al)))
            tbuf_put_json_xml(&o, v, al);
        tbuf_puts(&o, "\",\"type\":\"");
        v = xml_attr(ch, "Type", &al);
        tbuf_puts(&o, (v && al >= 6 && (v[0] == 'b' || v[0] == 'B')) ? "button" : "checkbox");
        tbuf_puts(&o, "\",\"memory\":[");

        int first_mem = 1, dropped = 0;
        const char *lc = close;
        while (lc < body_end && (lc = strstr(lc, "<Cheatline")) != NULL && lc < body_end) {
            const char *lclose = strstr(lc, "</Cheatline>");
            const char *lself  = strstr(lc, "/>");
            const char *lend;
            if (lclose && (!lself || lclose < lself)) lend = lclose + 12;
            else if (lself) lend = lself + 2;
            else break;

            size_t cl = (size_t)(lend - lc);
            if (cl >= SHN_CHUNK_MAX) { dropped++; lc = lend; continue; }   /* counted, not hidden */
            memcpy(chunk, lc, cl); chunk[cl] = 0;

            size_t ol = 0, onl = 0, offl = 0, abl = 0, scl = 0;
            const char *off  = xml_child(chunk, "Offset",   &ol);
            const char *on   = xml_child(chunk, "ValueOn",  &onl);
            const char *offv = xml_child(chunk, "ValueOff", &offl);
            const char *abs_ = xml_child(chunk, "Absolute", &abl);
            /* WHICH MODULE THE OFFSET IS IN. 770 cheatlines in the shipped library carry a non-zero
               one; this engine places the main executable only, so those cannot be applied, and the
               number is what lets the refusal say that instead of blaming the game's version. */
            const char *sect = xml_child(chunk, "Section", &scl);
            if (off && ol && (on || offv)) {
                if (!first_mem) tbuf_putc(&o, ',');
                first_mem = 0;
                tbuf_puts(&o, "{\"offset\":\"");
                tbuf_put_hex(&o, off, ol);
                tbuf_puts(&o, "\",\"on\":\"");
                if (on) tbuf_put_hex(&o, on, onl);
                tbuf_puts(&o, "\",\"off\":\"");
                if (offv) tbuf_put_hex(&o, offv, offl);
                tbuf_putc(&o, '"');
                if (abs_ && abl && (abs_[0] == '1' || abs_[0] == 't' || abs_[0] == 'T'))
                    tbuf_puts(&o, ",\"absolute\":true");
                if (sect && scl) {
                    tbuf_puts(&o, ",\"section\":\"");
                    tbuf_put_json(&o, sect, scl);
                    tbuf_putc(&o, '"');
                }
                tbuf_putc(&o, '}');
            }
            lc = lend;
        }
        tbuf_puts(&o, "]");
        if (dropped) {
            /* Read back by parse_mod_entries_ex: a mod with a dropped line is never applied and
               its state says so, instead of claiming ON for the part that was readable. */
            char dn[40];
            snprintf(dn, sizeof(dn), ",\"dropped\":%d", dropped);
            tbuf_puts(&o, dn);
        }
        tbuf_putc(&o, '}');
        cur = body_end + 8;
    }
    tbuf_puts(&o, "]}");
    free(chunk);
    return o.buf;
}

/* .mc4 -> XML. Returns malloc'd, NUL-terminated XML, or NULL. */
static char *mc4_to_xml(const char *cipher_b64, size_t len, size_t *xml_len_out) {
    static const uint8_t KEY[32] = "304c6528f659c766110239a51cl5dd9c";
    static const uint8_t IV[16]  = "u@}kzW2u[u(8DWar";
    size_t n = 0;
    uint8_t *raw = b64_decode(cipher_b64, len, &n);
    if (!raw) return NULL;
    if (n < 16 || (n % 16) != 0) { free(raw); return NULL; }
    aes256_cbc_decrypt(KEY, IV, raw, n);
    /* Strip PKCS#7 padding when it is well formed; tolerate files without it. */
    uint8_t pad = raw[n - 1];
    if (pad >= 1 && pad <= 16 && (size_t)pad <= n) {
        int ok = 1;
        for (int i = 0; i < pad; i++) if (raw[n - 1 - i] != pad) { ok = 0; break; }
        if (ok) n -= pad;
    }
    uint8_t *out = (uint8_t *)realloc(raw, n + 1);
    if (!out) { free(raw); return NULL; }
    out[n] = 0;
    if (xml_len_out) *xml_len_out = n;
    return (char *)out;
}

/* ---------------- loading a cheat file of any supported format ------------
 * Every format collapses onto the JSON document our engine already parses, so the
 * working JSON path is reused untouched. *non_json marks .shn/.mc4, whose offsets
 * need resolving (see cheat_addr_mode).
 * ---------------------------------------------------------------------- */
static int path_ext_is(const char *path, const char *ext) {
    size_t pl = strlen(path), el = strlen(ext);
    if (pl <= el) return 0;
    const char *p = path + pl - el;
    for (size_t i = 0; i < el; i++) {
        char a = p[i], b = ext[i];
        if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
        if (a != b) return 0;
    }
    return 1;
}

static char *cheat_load_doc(const char *path, int *non_json) {
    if (non_json) *non_json = 0;
    long len = 0;
    char *raw = slurp(path, &len);
    if (!raw || len <= 0) { free(raw); return NULL; }
    if (path_ext_is(path, ".shn")) {
        char *j = shn_xml_to_json(raw, (size_t)len);
        free(raw);
        if (non_json) *non_json = 1;
        return j;
    }
    if (path_ext_is(path, ".mc4")) {
        size_t xl = 0;
        char *xml = mc4_to_xml(raw, (size_t)len, &xl);
        free(raw);
        if (!xml) return NULL;
        char *j = shn_xml_to_json(xml, xl);
        free(xml);
        if (non_json) *non_json = 1;
        return j;
    }
    return raw;                     /* .json — already the shape we want */
}

/* ---------------- our own cheat library ----------------------------------
 * Everything the app needs lives under /data/pkg-mutant-shop. On first run we copy
 * across whatever is in the old CheatRunner tree. The source is left in place: the
 * copy costs nothing and it means a failed migration can never lose someone's cheats.
 * ~5000 small files is far too slow to block startup, so this runs on its own thread.
 * ------------------------------------------------------------------------- */
static char g_lib_status[160] = "not started";
static int  g_lib_migrated = 0;

static int file_exists(const char *p) {
    struct stat st;
    return stat(p, &st) == 0 && S_ISREG(st.st_mode);
}

/* HOW MUCH WE MOVE PER SYSCALL ON A BULK PATH, AND IT IS WORTH REAL THROUGHPUT.
 *
 * This was 65536, and that number was costing the console most of its network. Measured here
 * 2026-10-02, writing 128 MB to /mnt/ext1 over gigabit ethernet, changing nothing else:
 *
 *     64 KB ..... 42.7 MB/s      1 MB ..... 78.6 MB/s      4 MB ..... 75.8 MB/s
 *
 * The whole cost was syscall overhead: at 64 KB this path makes sixteen times as many read()/write()
 * pairs for the same bytes, and on this kernel each one is expensive enough to dominate. 4 MB is
 * slightly WORSE than 1 MB and costs four times the memory per worker, so the knee is here. Every
 * user of this allocates on the heap - worker stacks are small - so raising it is free of stack risk.
 *
 * With the socket buffers below, the same test now runs at 108.7 MB/s (872 Mbit/s), which is the
 * link, and the same speed this console reads at. Before all of this it was 42.7. */
#define COPY_BUF_BYTES (1 << 20)
static int copy_file(const char *src, const char *dst) {
    int in = open(src, O_RDONLY);
    if (in < 0) return -1;
    int out = open(dst, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (out < 0) { close(in); return -1; }
    /* Heap, not stack: this runs on a worker thread and 64 KB overruns its stack. */
    char *buf = (char *)malloc(COPY_BUF_BYTES);
    if (!buf) { close(in); close(out); return -1; }
    ssize_t r;
    int rc = 0;
    while ((r = read(in, buf, COPY_BUF_BYTES)) > 0) {
        ssize_t off = 0;
        while (off < r) {
            ssize_t w = write(out, buf + off, (size_t)(r - off));
            if (w <= 0) { rc = -1; break; }
            off += w;
        }
        if (rc) break;
    }
    free(buf);
    close(in); close(out);
    if (rc) unlink(dst);
    return rc;
}

/* Copy every regular file from src dir to dst dir, skipping ones already there. */
static int migrate_dir(const char *srcdir, const char *dstdir) {
    DIR *d = opendir(srcdir);
    if (!d) return 0;
    mkdir(dstdir, 0777);
    int copied = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        char sp[700], dp[700];
        snprintf(sp, sizeof(sp), "%s/%s", srcdir, e->d_name);
        snprintf(dp, sizeof(dp), "%s/%s", dstdir, e->d_name);
        if (file_exists(dp)) continue;
        if (!file_exists(sp)) continue;
        if (copy_file(sp, dp) == 0) copied++;
    }
    closedir(d);
    return copied;
}

static int count_dir(const char *dir) {
    DIR *d = opendir(dir);
    if (!d) return 0;
    int n = 0;
    struct dirent *e;
    while ((e = readdir(d))) if (e->d_name[0] != '.') n++;
    closedir(d);
    return n;
}


/* ---------------- autonomous intake of new cheat files -------------------
 * Drop a cheat file anywhere we watch and the app files it correctly by itself. The
 * format is decided by LOOKING INSIDE the file, not by trusting its extension — these
 * files are routinely renamed by whoever shares them. Watched locations are reported by
 * GET /api/cheat/paths so they can be found from a PC.
 * ------------------------------------------------------------------------- */
#define CHEAT_INBOX_DIR CHEAT_ROOT "/incoming"

/* "json" | "shn" | "mc4" | NULL. Sniffs content; mc4 is proven by actually decrypting. */
static const char *cheat_sniff(const char *path) {
    long len = 0;
    char *data = slurp(path, &len);
    if (!data || len < 8) { free(data); return NULL; }
    const char *kind = NULL;
    const char *p2 = data;
    while (*p2 == ' ' || *p2 == 0x0A || *p2 == 0x0D || *p2 == 0x09 ||
           (unsigned char)*p2 == 0xEF || (unsigned char)*p2 == 0xBB ||
           (unsigned char)*p2 == 0xBF) p2++;   /* whitespace + UTF-8 BOM */
    if (*p2 == '{') kind = "json";
    /* A GAME PATCH XML, AND IT HAS TO BE TESTED BEFORE THE GENERIC "<?xml" FALLBACK. Every patch
       document is XML, so the fallback below claimed all 376 of them as "shn" - they were filed into
       shn/ as <TID>.shn, which removed them from CHEAT_PATCH_DIR entirely AND, because a generic file
       outranks an other-version match, made them outrank the real versioned trainer for 200 titles.
       Safe by measurement, not by hope: 0 of the 5,193 cheat files in the shipped library contain
       "<Patch" or "<TitleID>", and all 376 patch documents do. */
    else if (strstr(data, "<Patch") || strstr(data, "<TitleID>")) kind = "patch";
    else if (strstr(data, "<Trainer") || strstr(data, "<?xml")) kind = "shn";
    else {
        /* Base64-looking? Only a successful decrypt to Trainer XML proves it is .mc4. */
        int looks_b64 = 1;
        for (long i = 0; i < len && i < 256; i++) {
            char c = data[i];
            if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9')
                  || c == '+' || c == '/' || c == '=' || c == 0x0A ||
                  c == 0x0D)) { looks_b64 = 0; break; }
        }
        if (looks_b64) {
            size_t xl = 0;
            char *xml = mc4_to_xml(data, (size_t)len, &xl);
            if (xml) {
                if (strstr(xml, "<Trainer")) kind = "mc4";
                free(xml);
            }
        }
    }
    free(data);
    return kind;
}

static int move_file(const char *src, const char *dst) {
    if (rename(src, dst) == 0) return 0;          /* same filesystem: instant */
    if (copy_file(src, dst) != 0) return -1;      /* across devices (USB): copy then drop */
    unlink(src);
    return 0;
}

/* Sort every recognisable cheat file out of one directory into our library.
   Returns how many were filed. */
/* FILE A GAME PATCH UNDER EVERY TITLE IT COVERS, and count it once.
 *
 * patch_file_for() only ever stats CHEAT_PATCH_DIR/<TID>.xml, so a patch document is reachable only
 * under a name that is a title id. The sender's filename will not do: measured over all 376 shipped
 * patches, 303 cover more than one title and the filename matches the FIRST <ID> in only 156 of them,
 * while the filename appears somewhere among the <ID>s in all 376. Filing under the first id alone
 * would therefore leave 220 titles with no patch file at all, and look like it had worked.
 *
 * THE FAN-OUT CANNOT USE move_file(). rename() takes the source away on the first copy and copies
 * 2..n then have nothing to read - so every destination is COPIED, and the source is removed once at
 * the end and only when the caller asked for a move. The return value is 0 or 1, never n: this is one
 * document being filed, and reporting "filed 5" for one file is a lie the owner would act on.
 */
static int cheat_intake_patch(const char *sp, int move_it) {
    long len = 0;
    char *doc = slurp(sp, &len);
    if (!doc) return 0;
    int wrote = 0;
    const char *p = doc;
    while ((p = strstr(p, "<ID>")) != NULL) {
        p += 4;
        const char *e = strchr(p, '<');
        if (!e) break;
        size_t n = (size_t)(e - p);
        if (n == 0 || n >= 24) continue;                  /* not a title id */
        char id[24];
        memcpy(id, p, n);
        id[n] = 0;
        int ok = 1;
        for (size_t i = 0; i < n; i++) {
            char c = id[i];
            if (!((c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                  (c >= '0' && c <= '9'))) { ok = 0; break; }
        }
        if (!ok) continue;                                /* whatever that was, it is not an id */
        char dp[700];
        snprintf(dp, sizeof(dp), "%s/%s.xml", CHEAT_PATCH_DIR, id);
        /* SAME PATH: already where it belongs. Falling through would unlink it and then copy from a
           file that no longer exists. */
        if (!strcmp(dp, sp)) { wrote++; continue; }
        if (file_exists(dp)) unlink(dp);                  /* a re-drop is an intentional replace */
        if (copy_file(sp, dp) == 0) wrote++;
    }
    free(doc);
    if (wrote && move_it) unlink(sp);
    return wrote ? 1 : 0;
}

static int cheat_intake_dir(const char *dir, int move_it, int depth) {
    DIR *d = opendir(dir);
    if (!d) return 0;
    int filed = 0;
    struct dirent *e;
    while ((e = readdir(d))) {
        if (e->d_name[0] == '.') continue;
        char sp[700];
        snprintf(sp, sizeof(sp), "%s/%s", dir, e->d_name);

        /* ONE LEVEL DOWN, into a folder named like the library's own. A stick or a /data/cheats that
           holds a COPY of the library - json/ shn/ mc4/ patches/ - used to report "filed 0" and file
           nothing, because every entry here is a directory and file_exists() rejects it. One level
           only, and only those names, so this can never walk somebody's whole drive.
           S_ISDIR via stat, not dirent's d_type: nothing else in this file relies on d_type and it is
           not dependable across filesystems. */
        struct stat est;
        if (depth == 0 && stat(sp, &est) == 0 && S_ISDIR(est.st_mode)) {
            if (!strcmp(e->d_name, "json") || !strcmp(e->d_name, "shn") ||
                !strcmp(e->d_name, "mc4")  || !strcmp(e->d_name, "patches") ||
                !strcmp(e->d_name, "incoming"))
                filed += cheat_intake_dir(sp, move_it, 1);
            continue;
        }

        if (!file_exists(sp)) continue;
        const char *kind = cheat_sniff(sp);
        if (!kind) continue;
        if (!strcmp(kind, "patch")) { filed += cheat_intake_patch(sp, move_it); continue; }
        const char *destdir = !strcmp(kind, "shn") ? CHEAT_SHN_DIR
                            : !strcmp(kind, "mc4") ? CHEAT_MC4_DIR : CHEAT_JSON_DIR;
        /* Keep the sender's name (it carries TITLEID_VERSION) but force the true extension. */
        char stem[300];
        snprintf(stem, sizeof(stem), "%s", e->d_name);
        char *dot = strrchr(stem, '.');
        if (dot && (path_ext_is(stem, ".json") || path_ext_is(stem, ".shn") ||
                    path_ext_is(stem, ".mc4")  || path_ext_is(stem, ".txt"))) *dot = 0;
        char dp[700];
        snprintf(dp, sizeof(dp), "%s/%s.%s", destdir, stem, kind);
        /* ALREADY WHERE IT BELONGS - which is the normal case once this descends into a library-shaped
           folder. Without this, the unlink below deletes the file and the copy that follows fails. */
        if (!strcmp(dp, sp)) continue;
        if (file_exists(dp)) unlink(dp);          /* a re-drop is an intentional replace */
        if ((move_it ? move_file(sp, dp) : copy_file(sp, dp)) == 0) filed++;
    }
    closedir(d);
    return filed;
}

/* Everywhere we look for dropped-in cheats. USB first-class: plug a stick in with a
   /cheats folder and the console files them on its own. */
static int cheat_intake_all(void) {
    int n = cheat_intake_dir(CHEAT_INBOX_DIR, 1, 0);
    n += cheat_intake_dir("/data/cheats", 1, 0);
    for (int i = 0; i < 8; i++) {
        char up[64];
        snprintf(up, sizeof(up), "/mnt/usb%d/cheats", i);
        n += cheat_intake_dir(up, 0, 0);          /* copy from USB — never delete the user's stick */
    }
    return n;
}

/* Write the embedded cheat library out to disk, skipping anything already there.

   The library ships INSIDE this ELF (7022 files) so installing the app is all the user ever has to
   do - no separate download, no copying a folder to the console. The cost is that the payload
   carries ~32 MB which the loader maps into RAM; that is the deliberate trade for self-containment.

   Extraction is incremental and idempotent: a file that already exists is left alone, so the first
   boot writes the library and every later boot costs one stat() per entry and writes nothing.
   The one exception is a ZERO-byte file: open(O_CREAT) used to create the name before the write
   loop, so a /data that ran out of space mid-extract left an empty file that passed the exists
   test on every later boot and was never repaired. Now the bytes go to <name>.part and are
   renamed into place only when complete, and an empty file on disk is treated as missing.
   (The xml* folders are still created here, harmlessly; the bundle no longer ships them - see
   gen_cheat_bundle.py - because only CHEAT_PATCH_DIR is ever read.)
   Returns the number of files actually created. */
static int cheat_bundle_extract(void) {
    static const char *SUBS[] = { "json", "mc4", "shn", "patches", "xml", "xml_orbis", "xml_prospero" };
    for (unsigned i = 0; i < sizeof(SUBS) / sizeof(SUBS[0]); i++) {
        char d[256];
        snprintf(d, sizeof(d), "%s/%s", CHEAT_ROOT, SUBS[i]);
        mkdir(d, 0777);
    }
    int wrote = 0;
    for (int i = 0; i < CHEAT_FILES_COUNT; i++) {
        const cheat_file_t *e = &CHEAT_FILES[i];
        char out[320], part[330];
        snprintf(out, sizeof(out), "%s/%s", CHEAT_ROOT, e->path);
        struct stat st;
        if (stat(out, &st) == 0 && st.st_size > 0) continue;   /* already on disk - never overwrite */
        snprintf(part, sizeof(part), "%s.part", out);
        int fd = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0777);
        if (fd < 0) continue;
        const unsigned char *src = cb_pack + e->off;
        unsigned left = e->len;
        while (left) {
            int w = (int)write(fd, src, left);
            if (w <= 0) break;
            src += w; left -= (unsigned)w;
        }
        close(fd);
        if (left == 0 && rename(part, out) == 0) wrote++;
        else unlink(part);                                   /* nothing half-written is left behind */
    }
    return wrote;
}

static void *cheat_library_thread(void *arg) {
    (void)arg;
    snprintf(g_lib_status, sizeof(g_lib_status), "migrating");
    mkdir(SHOP_DATA_DIR, 0777);
    mkdir(CHEAT_ROOT, 0777);
    mkdir(CHEAT_JSON_DIR, 0777);
    mkdir(CHEAT_SHN_DIR, 0777);
    mkdir(CHEAT_MC4_DIR, 0777);
    mkdir(CHEAT_PATCH_DIR, 0777);
    int j = migrate_dir(LEGACY_CHEAT_ROOT "/json", CHEAT_JSON_DIR);
    int h = migrate_dir(LEGACY_CHEAT_ROOT "/shn",  CHEAT_SHN_DIR);
    int m = migrate_dir(LEGACY_CHEAT_ROOT "/mc4",  CHEAT_MC4_DIR);
    int p = migrate_dir(LEGACY_PATCH_DIR "/xml_prospero", CHEAT_PATCH_DIR);
    p    += migrate_dir(LEGACY_PATCH_DIR "/xml_orbis",    CHEAT_PATCH_DIR);
    p    += migrate_dir(LEGACY_PATCH_DIR "/xml",          CHEAT_PATCH_DIR);
    mkdir(CHEAT_INBOX_DIR, 0777);
    int shipped = cheat_bundle_extract();         /* ship-with-the-app: lay down what is missing */
    int filed = cheat_intake_all();               /* anything dropped in since last boot */
    snprintf(g_lib_status, sizeof(g_lib_status),
             "ready (shipped=%d copied json=%d shn=%d mc4=%d patches=%d, filed %d dropped)",
             shipped, j, h, m, p, filed);
    g_lib_migrated = 1;
    if (shipped > 0)
        notifyf("Cheat library is ready\n%d file%s came with the app - there is nothing to download",
                shipped, shipped == 1 ? "" : "s");
    notify_cheats_filed(filed);
    return NULL;
}

static void cheat_library_bootstrap(void) {
    pthread_t t;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 256 * 1024);   /* default worker stacks here are tiny */
    int rc = pthread_create(&t, &attr, cheat_library_thread, NULL);
    pthread_attr_destroy(&attr);
    if (rc == 0) pthread_detach(t);
    else snprintf(g_lib_status, sizeof(g_lib_status), "could not start migration thread");
}

/* Best cheat file for a title, across every format we support. An exact-version match beats
   the generic <TID> file, which beats any other version. Within the same quality JSON wins,
   then .shn, then .mc4 — JSON offsets are unambiguous, so it needs no address probing.
   Falls back to the old CheatRunner tree while the migration thread is still copying.
   Returns 1 only when the exact version matched. */
static int cheat_pick_file(const char *tid, const char *ver, char *out, size_t outsz,
                           const char **why) {
    static const char *EXTS[3] = { ".json", ".shn", ".mc4" };
    const char *dirs[6] = { CHEAT_JSON_DIR, CHEAT_SHN_DIR, CHEAT_MC4_DIR,
                            LEGACY_CHEAT_ROOT "/json", LEGACY_CHEAT_ROOT "/shn",
                            LEGACY_CHEAT_ROOT "/mc4" };
    char best[6][600], generic[6][600], anyv[6][600];
    memset(best, 0, sizeof(best));
    memset(generic, 0, sizeof(generic));
    memset(anyv, 0, sizeof(anyv));
    size_t tl = strlen(tid);
    for (int k = 0; k < 6; k++) {
        const char *ext = EXTS[k % 3];
        size_t el = strlen(ext);
        char want[96] = {0}, gname[96];
        if (ver && ver[0]) snprintf(want, sizeof(want), "%s_%s%s", tid, ver, ext);
        snprintf(gname, sizeof(gname), "%s%s", tid, ext);
        DIR *d = opendir(dirs[k]);
        if (!d) continue;
        struct dirent *e;
        while ((e = readdir(d))) {
            const char *n = e->d_name;
            if (strncmp(n, tid, tl)) continue;
            size_t l = strlen(n);
            if (l <= el || !path_ext_is(n, ext)) continue;
            if (want[0] && !strcmp(n, want))  snprintf(best[k],    sizeof(best[k]),    "%s/%s", dirs[k], n);
            else if (!strcmp(n, gname))       snprintf(generic[k], sizeof(generic[k]), "%s/%s", dirs[k], n);
            else if (!anyv[k][0])             snprintf(anyv[k],    sizeof(anyv[k]),    "%s/%s", dirs[k], n);
        }
        closedir(d);
    }
    for (int k = 0; k < 6; k++) if (best[k][0]) {
        snprintf(out, outsz, "%s", best[k]); if (why) *why = "exact version"; return 1; }
    for (int k = 0; k < 6; k++) if (generic[k][0]) {
        snprintf(out, outsz, "%s", generic[k]); if (why) *why = "generic file"; return 0; }
    for (int k = 0; k < 6; k++) if (anyv[k][0]) {
        snprintf(out, outsz, "%s", anyv[k]); if (why) *why = "other version"; return 0; }
    snprintf(out, outsz, "%s", "");
    if (why) *why = "none";
    return 0;
}

/* Every version we hold a cheat file for, for one title. THIS FEEDS THE VERSION PICKER, and since
   3.83.2 the picker is the only thing on the panel that names the other versions at all - the two
   notes that used to say it in a sentence were removed at the owner's request. So the stated reason
   for this function is not "a note" any more; it is the control the owner presses to load another
   version's cheats. Deleting it takes the picker with it.

   Only names in a format the engine opens count, and exactly that extension is cut off. The
   version used to be whatever sat between the underscore and the LAST dot, with no look at the
   extension, so the 706 decrypted '<TID>_<ver>.mc4.xml' twins in mc4/ (never readable by the
   engine - cheat_pick_file skips them for the same reason) produced versions like "01.00.mc4"
   in the panel's "cheats also exist for ..." note. */
static void cheat_versions_json(const char *tid, char *out, size_t outsz) {
    static const char *DIRS[3] = { CHEAT_JSON_DIR, CHEAT_SHN_DIR, CHEAT_MC4_DIR };
    static const char *EXTS[3] = { ".json", ".shn", ".mc4" };
    size_t l = 0;
    int first = 1;
    l += snprintf(out + l, outsz - l, "[");
    size_t tl = strlen(tid);
    for (int k = 0; k < 3 && l < outsz - 40; k++) {
        DIR *d = opendir(DIRS[k]);
        if (!d) continue;
        size_t el = strlen(EXTS[k]);
        struct dirent *e;
        while ((e = readdir(d)) && l < outsz - 40) {
            const char *n = e->d_name;
            if (strncmp(n, tid, tl)) continue;
            if (n[tl] != '_') continue;                 /* <TID>_<version>.<ext> only */
            if (!path_ext_is(n, EXTS[k])) continue;     /* .mc4.xml and friends are not versions */
            size_t nl = strlen(n);
            if (nl <= tl + 1 + el) continue;
            char ver[32] = {0};
            size_t vl = nl - el - (tl + 1);             /* between the underscore and the extension */
            if (vl >= sizeof(ver)) continue;
            memcpy(ver, n + tl + 1, vl); ver[vl] = 0;
            if (!ver[0]) continue;
            int dup = 0;
            char needle[40];
            snprintf(needle, sizeof(needle), "\"%s\"", ver);
            if (strstr(out, needle)) dup = 1;
            if (dup) continue;
            l += snprintf(out + l, outsz - l, "%s\"%s\"", first ? "" : ",", ver);
            first = 0;
        }
        closedir(d);
    }
    snprintf(out + l, outsz - l, "]");
}

/* ---------------- where a .shn/.mc4 offset actually points ----------------
 * JSON offsets are always image-relative. The XML formats are not: most are relative but
 * some are absolute, and nothing in the file reliably says which. We settle it by READING
 * both candidates and keeping whichever already holds a state the file documents.
 *
 * Two details matter. Relative is probed first because a raw absolute offset can land on
 * GPU/MMIO and hang the game on a mere read. And an entry whose ValueOff is a run of one
 * repeated byte (a zero-filled code cave — 38% of entries in the real library) cannot
 * discriminate, so the mode is decided from the mod's distinctive entries and then applied
 * to the rest. Returns 0 = relative, 1 = absolute.
 * ------------------------------------------------------------------------- */
static int entry_discriminating(const cheat_entry_t *e) {
    if (e->off_len < 4) return 0;
    for (int i = 1; i < e->off_len; i++) if (e->off[i] != e->off[0]) return 1;
    return 0;
}

static int cheat_addr_mode(const cheat_entry_t *es, int n, pid_t pid, intptr_t base) {
    unsigned char cur[CHEAT_MAX_BYTES];
    for (int i = 0; i < n; i++) {
        const cheat_entry_t *e = &es[i];
        if (!entry_discriminating(e)) continue;
        int len = e->off_len;
        intptr_t rel = base + (intptr_t)e->offset;
        intptr_t abs_ = (intptr_t)e->offset;
        if (ADDR_OK(rel) && mem_read(pid, rel, cur, (size_t)len) == 0) {
            if (memcmp(cur, e->off, (size_t)len) == 0) return 0;
            if (e->on_len == len && memcmp(cur, e->on, (size_t)len) == 0) return 0;
        }
        if (ADDR_OK(abs_) && mem_read(pid, abs_, cur, (size_t)len) == 0) {
            if (memcmp(cur, e->off, (size_t)len) == 0) return 1;
            if (e->on_len == len && memcmp(cur, e->on, (size_t)len) == 0) return 1;
        }
    }
    /* Nothing conclusive: relative is both the overwhelmingly common case and the safe
       choice — expect-gating still refuses any write whose target does not already hold a
       documented state, so a wrong guess fails closed instead of corrupting the game. */
    return 0;
}

static intptr_t cheat_entry_addr(const cheat_entry_t *e, intptr_t base, int abs_mode) {
    return (e->absolute || abs_mode) ? (intptr_t)e->offset : base + (intptr_t)e->offset;
}


static void rewrite_for_install(const char *in, char *out, size_t outsz);
static int install_pkg_local(const char *path, char *cid_out, size_t cid_sz);

static void handle(int fd, const char *rawpath, const char *req);
static const char *strcasestr_local(const char *hay, const char *needle);
static int pm_get(const char *path);   /* Payload Manager :8084 - spawns our installer */
static int pm_running(const char *filename);  /* ...and answers which payloads are alive */
static int hb_list_json(char *out, size_t outsz);  /* homebrew packages this console can reach */
static void pm_stem(const char *name, char *out, size_t outsz);  /* comparable payload name */
static int udp_port_taken(int port);              /* the only way to see a UDP service */
static int pb_stage_for_pldmgr(const char *name, char *out, size_t outsz);  /* see the basename trap */
static int pb_downloaded_find(const char *want_stem, char *name, size_t namesz,
                              long long *size, int skip);
static int app_ids_json(char *out, size_t outsz);  /* ...and the title ids it already has */
/* Only one spawned install at a time: they share one request file.

   The comment that used to sit here said "not a mutex - the accept loop is single-threaded for
   these routes". That premise was false. spawn_install_wait() runs on localinst_thread, a worker
   started by /api/install, while /api/engine/install-spawn runs on the accept thread, and both
   did test-then-set on this flag with nothing between the test and the set. Two overlapping
   installs then shared one installer-req.txt and the second installer installed whatever the
   first had asked for - the duplicate-install shape that has already taken this console down.
   The check-and-set is now one indivisible step under g_spawn_lock. The latch still EXPIRES
   (SPAWN_STALE_SECS) rather than deadlocking a queue behind a spawned process that died without
   writing a verdict. */
static pthread_mutex_t g_spawn_lock = PTHREAD_MUTEX_INITIALIZER;
static volatile int  g_spawn_busy = 0;
static volatile long g_spawn_started = 0;
/* Which request the verdict on disk belongs to. A late verdict from a PREVIOUS installer - one
   still pre-allocating a large package when its lane gave up - used to be accepted as this job's
   the moment it appeared, because the readers matched on presence and "ok":true alone. That is a
   false success, or a false refusal carrying the wrong content id. The token goes out as line 3 of
   installer-req.txt and comes back as "token" in installer-res.json; a verdict without it is not
   ours. Every existing field and file name is unchanged. */
static volatile long long g_spawn_token = 0;
#define SPAWN_STALE_SECS 600
#define SPAWN_REQ_PATH   SHOP_DATA_DIR "/installer-req.txt"
#define SPAWN_RES_PATH   SHOP_DATA_DIR "/installer-res.json"

/* Claim the lane. 0 with the lane held and this request's token in *token_out, or -1 while another
   hand-off is genuinely live. BOTH lanes come through here, so they cannot drift apart: a verdict
   on disk means the previous installer has already exited, and that releases the latch at once
   instead of making the next install wait out the expiry for a dead process - a rule the async
   lane applied and the console-local lane did not. */
static int spawn_lane_claim(long long *token_out) {
    pthread_mutex_lock(&g_spawn_lock);
    if (g_spawn_busy && access(SPAWN_RES_PATH, F_OK) == 0) {
        ilog("install: previous verdict was never collected - releasing the lane");
        g_spawn_busy = 0;
    }
    if (g_spawn_busy && (time(NULL) - g_spawn_started) < SPAWN_STALE_SECS) {
        pthread_mutex_unlock(&g_spawn_lock);
        return -1;
    }
    g_spawn_busy = 1;
    g_spawn_started = time(NULL);
    g_spawn_token = now_ms_local();          /* monotonic ms: unique per request, never reused */
    if (token_out) *token_out = g_spawn_token;
    pthread_mutex_unlock(&g_spawn_lock);
    return 0;
}

static void spawn_lane_release(void) {
    pthread_mutex_lock(&g_spawn_lock);
    g_spawn_busy = 0;
    pthread_mutex_unlock(&g_spawn_lock);
}

/* The spawn-status rule, for anything else that needs to know whether a hand-off is live RIGHT
   NOW: latched, no verdict on disk yet, and younger than the expiry. */
static int spawn_lane_live(void) {
    pthread_mutex_lock(&g_spawn_lock);
    int live = g_spawn_busy && access(SPAWN_RES_PATH, F_OK) != 0 &&
               (time(NULL) - g_spawn_started) < SPAWN_STALE_SECS;
    pthread_mutex_unlock(&g_spawn_lock);
    return live;
}

/* Does this verdict carry OUR token? A result with no token at all (an installer launched by hand,
   or one that predates the token) never matches a request that has one. */
static int spawn_verdict_is_ours(const char *json, long long token) {
    char needle[48];
    snprintf(needle, sizeof(needle), "\"token\":\"%lld\"", token);
    return strstr(json, needle) != NULL;
}

/* Credential profile applied around ONE install call and restored on every exit path.
   Declared here because the HTTP handler builds one long before the installer is defined. */
typedef struct {
    unsigned long long authid;      /* 0 = leave alone */
    int  set_jaildir, jaildir_null; /* set_jaildir=1 -> apply jaildir_null */
    int  set_caps;   unsigned char caps[16];
    int  set_uids;   int uid_val;
} cred_profile_t;

static int install_by_package_inproc_ex2(const char *uri, char *cid_out, size_t cid_sz,
                                         int announce_bigapp, int ctype, int cplat,
                                         const cred_profile_t *cp);

/* ---------------- who is allowed to drive this server -----------------------------------------
 * This process answers on INADDR_ANY:8710 with `Access-Control-Allow-Origin: *`, as root, and some
 * of its routes install packages and write memory. The PC companion has guarded itself this way
 * since it grew a web UI; this end never did, so any page open in any browser on the network could
 * drive it.
 *
 * The discriminator is NOT "same origin" - the console legitimately serves its UI to itself while
 * the data comes from a PC on another address, and that is the whole architecture. It is whether
 * the requesting page has a DNS hostname at all: every legitimate caller here is a private-range
 * IP literal or loopback, and a hostile public site always has a name.
 *
 * A request with NEITHER header is allowed on purpose - that is curl, the companion, and our own
 * tooling, none of which a remote page can forge (a browser always attaches one).
 */
static int host_is_private(const char *h, size_t len) {
    char b[80];
    size_t j = 0;
    for (size_t i = 0; i < len && j < sizeof(b) - 1; i++) {
        if (h[i] == ':' || h[i] == '/') break;          /* strip :port and any path */
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
 * request_origin_ok() lets a request with no Origin and no Referer through on purpose - that is
 * curl, the companion and our own tools. But a browser can be made to send neither: a page with a
 * no-referrer policy loading a state-changing GET as an <img>, a <script> or a top-level navigation
 * carries no Referer at all, and that walked straight past the guard. Sec-Fetch-Mode and
 * Sec-Fetch-Dest cannot be suppressed or forged by a page, so on the routes that CHANGE something
 * they are consulted as well: a navigation, a subresource load or a no-cors fetch is refused.
 * ABSENT headers are allowed - older WebKit, curl and urllib send none - and everything the UI
 * does is fetch(), mode "cors" or "same-origin", so nothing the app itself does changes.
 * Read-only routes are not consulted at all. */
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

/* The routes that change something on the console - the only ones sec_fetch_ok() is applied to.
   `rawpath` still carries its ?query: /api/payloads/autostart only WRITES when it has one. */
static int route_changes_state(const char *rawpath, int is_post) {
    static const char *EXACT[] = {
        "/api/quit", "/api/power", "/api/rest/prepare", "/api/fs/delete", "/api/fs/mkdir",
        "/api/fs/write", "/api/mem/write", "/api/cheat/apply", "/api/patch/apply",
        "/api/patch/revert", "/api/engine/cancel", "/api/engine/spawn-cleanup",
        "/api/engine/fetch", "/api/tile/install", "/api/game/delete-backup", "/api/game/delete",
        "/api/move", "/api/cheat/rescan", "/api/cheats/rescan", "/api/register-pc",
        "/api/open", "/api/notify", "/api/install", "/api/payloads/load", NULL
    };
    char path[1024];
    snprintf(path, sizeof(path), "%s", rawpath);
    char *qs = strchr(path, '?');
    if (qs) *qs = 0;
    for (int i = 0; EXACT[i]; i++) if (!strcmp(path, EXACT[i])) return 1;
    if (!strncmp(path, "/api/engine/install-", 20)) return 1;
    if (!strcmp(path, "/api/payloads/autostart") && qs) return 1;
    /* The POST verbs that write: a cheat toggle, a queue start/cancel/retry. */
    if (is_post && (!strncmp(path, "/api/mods/", 10) || !strncmp(path, "/api/queue", 10))) return 1;
    return 0;
}

/* ---------------- routes that can install, behind an explicit opt-in ---------------------------
 * install-inproc / install-local / install-dir / diag call libSceAppInstUtil directly and are
 * hand-fired diagnostics: nothing in the UI and nothing in the companion calls them (`diag`
 * performs TWO real installs of the same package). They stay in the build because they are how the
 * install engine gets debugged, but they are no longer available just for being on the network.
 *
 * Touch /data/pkg-mutant-shop/allow-diagnostics to enable them for this boot; delete it to stop.
 * install-url (the in-process download+install half of dl_worker, which nothing in the product
 * calls any more) and lprobe sit behind the same flag. Every read-only route is NOT gated.
 */
#define DIAG_FLAG SHOP_DATA_DIR "/allow-diagnostics"

/* ---------------- can this console reach an HTTPS host at all? ------------------------------
   WHAT THIS MEASURED, so the next attempt does not start from nothing.

   Downloading and updating from the console with no PC both need GitHub, and GitHub is HTTPS
   only. This build links -lSceSsl -lSceHttp and had never called either. Driven from the console
   through the gated /api/net/https route below:

     http://neverssl.com/            200, 3961 bytes   - DNS, routing and sceHttp all work
     http://10.0.0.76:8710/...       net error 0x80431068 (that PC was off - the right answer)
     https://example.com/            sceHttpSendRequest -> 0x8095f00c
     https://raw.githubusercontent.com/...   the same, every time

   And it is not any of the obvious things. Every handle is valid before the failure
   (net=3000 ssl=1 http=257, real template/connection/request ids), so sceSslInit and sceHttpInit
   genuinely succeed; loading the console's certificate store changes nothing; turning server
   verification off changes nothing; pinning TLS 1.2 changes nothing; loading libSceSsl.sprx and
   libSceHttp.sprx into this process first changes nothing. The failure is inside the handshake.

   So plain HTTP from this payload works and TLS does not, and that is why the panel still says a
   PC has to fetch a download. This route is kept - behind the diagnostics flag, reaching the
   internet from a console is not something to leave switched on - because it is the instrument
   that produced the table above and the one the next attempt will need.

   Declared here rather than pulled from a header the SDK does not ship. */
int sceNetPoolCreate(const char *name, int size, int flags);
int sceNetPoolDestroy(int memid);
int sceSslInit(unsigned long poolSize);
int sceSslTerm(int sslCtxId);
int sceHttpInit(int netMemId, int sslCtxId, unsigned long poolSize);
int sceHttpTerm(int httpCtxId);
int sceHttpCreateTemplate(int httpCtxId, const char *userAgent, int httpVer, int autoProxyConf);
int sceHttpDeleteTemplate(int tmplId);
int sceHttpCreateConnectionWithURL(int tmplId, const char *url, int enableKeepalive);
int sceHttpDeleteConnection(int connId);
int sceHttpCreateRequestWithURL(int connId, int method, const char *url, unsigned long long len);
int sceHttpDeleteRequest(int reqId);
int sceHttpSendRequest(int reqId, const void *postData, unsigned long size);
int sceHttpGetStatusCode(int reqId, int *statusCode);
int sceHttpReadData(int reqId, void *data, unsigned long size);
int sceHttpSetAutoRedirect(int id, int enable);
int sceHttpSetConnectTimeOut(int id, unsigned int usec);
int sceHttpSetRecvTimeOut(int id, unsigned int usec);
int sceHttpsDisableOption(int tmplId, unsigned int flags);
int sceHttpsLoadCert(int httpCtxId, int caCertNum, const void *caList,
                     const void *cert, const void *privKey);
int sceHttpsSetMinSslVersion(int id, int version);
int sceHttpsSetSslVersion(int id, int version);
int sceHttpsSetSslCallback(int id, void *cbfunc, void *userArg);
/* Verified present in the SDK's libSceHttp.so before being declared here - guessing at a
   signature has crashed this console once already. */
int sceHttpAddRequestHeader(int reqId, const char *name, const char *value, unsigned int mode);
int sceHttpGetResponseContentLength(int reqId, int *result, unsigned long long *contentLength);
int sceHttpSetSendTimeOut(int id, unsigned int usec);

/* Accept whatever the far end presents. Used ONLY by the probe, to answer "is this a
   verification failure or something else" - a question the single 0x01 flag did not settle. */
/* THE ONE CALL THAT MAKES TLS WORK HERE - see the note above https_get_mode.
   DEREFERENCES NOTHING, DELIBERATELY. The real callback is
     int (*)(int libsslCtxId, unsigned int verifyErr, SceSslCert *const certs[], int certNum,
             void *userArg)
   and this is handed over as a void*, so the arguments arriving here are not the ones named
   below. Writing through any of them would be writing through whatever the system happened to
   pass in that register - on the PS4 this code runs inside a shared system daemon, and that is
   how a console loses its shop. Returning 0 is the whole job. */
static int pms_ssl_accept_cb(void *a, void *b, void *c, int d) {
    (void)a; (void)b; (void)c; (void)d;
    return 0;
}
/* Declared again here: the installer's copy of this prototype sits further down the file. */
int sceKernelLoadStartModule(const char *name, size_t argc, const void *argv,
                             uint32_t flags, void *opt, int *pRes);

static char g_https_ids[128] = {0};  /* the last probe's handles, for the diagnostic reply */

/* One GET over TLS into a caller-sized buffer. Returns the HTTP status, or a negative SCE error.
   `out` may be NULL to count the bytes without keeping them. Every handle is released on every
   path, including the failures - a payload that leaks an HTTP context has no second chance. */
/* `mode` is for the probe only: 0 as shipped, 1 also loads the console's certificate store,
   2 also turns server verification off. Which of those is actually needed is a question for the
   console, not for a guess. */
static int https_get_mode(const char *url, char *out, size_t outsz, long long *got, char *err,
                          size_t errsz, int mode) {
    int netmem = -1, sslctx = -1, httpctx = -1, tmpl = -1, conn = -1, req = -1;
    int status = -1, rc;
    long long total = 0;
    if (got) *got = 0;
    if (err && errsz) err[0] = 0;
#define PMS_HTTP_FAIL(what, code) do { \
        if (err && errsz) snprintf(err, errsz, "%s=0x%x", (what), (unsigned)(code)); \
        goto done; } while (0)

    /* mode 3: load the TLS modules into THIS process first. The payload is injected into a host
       that may never have needed them - the installer in this file already loads its own .sprx
       the same way - and a stub that resolves to nothing would fail exactly like this. */
    if (mode >= 3) {
        int r1 = -1, r2 = -1, res = 0;
        r1 = sceKernelLoadStartModule("/system/common/lib/libSceSsl.sprx", 0, NULL, 0, NULL, &res);
        r2 = sceKernelLoadStartModule("/system/common/lib/libSceHttp.sprx", 0, NULL, 0, NULL, &res);
        if (err && errsz) snprintf(err, errsz, "ssl=0x%x http=0x%x", (unsigned)r1, (unsigned)r2);
    }
    /* mode 7 and up: a certificate chain is not small, and the sizes below were a guess. */
    int _np = (mode >= 7) ? (1024 * 1024) : (64 * 1024);
    int _sp = (mode >= 7) ? (4 * 1024 * 1024) : (512 * 1024);
    int _hp = (mode >= 7) ? (4 * 1024 * 1024) : (512 * 1024);
    netmem = sceNetPoolCreate("pms-https", _np, 0);
    if (netmem < 0) PMS_HTTP_FAIL("netpool", netmem);
    sslctx = sceSslInit(_sp);
    if (sslctx < 0) PMS_HTTP_FAIL("sslinit", sslctx);
    httpctx = sceHttpInit(netmem, sslctx, _hp);
    if (httpctx < 0) PMS_HTTP_FAIL("httpinit", httpctx);
    if (mode >= 1) {
        int lc = sceHttpsLoadCert(httpctx, 0, NULL, NULL, NULL);
        if (err && errsz && lc < 0) snprintf(err, errsz, "loadcert=0x%x", (unsigned)lc);
    }
    tmpl = sceHttpCreateTemplate(httpctx, "PKG-MUTANT-SHOP/1.0", 1 /* HTTP/1.1 */, 1);
    if (tmpl < 0) PMS_HTTP_FAIL("template", tmpl);
    /* A release asset is a redirect to objects.githubusercontent.com; without this the body is
       the redirect page. */
    sceHttpSetAutoRedirect(tmpl, 1);
    /* 0x01 is SCE_HTTPS_FLAG_SERVER_VERIFY. */
    if (mode >= 2) sceHttpsDisableOption(tmpl, 0x01);
    if (mode == 5 || mode >= 8) {
        /* THE WHOLE VERIFICATION FAMILY, not just the first flag. 0x01 server-verify,
           0x02 client-verify, 0x04 name check, 0x08 not-after, 0x10 not-before, 0x20 known-CA.
           Disabling only 0x01 leaves four checks that fail the same way. */
        int dv = sceHttpsDisableOption(tmpl, 0x3F);
        if (err && errsz) snprintf(err, errsz, "disableall=0x%x", (unsigned)dv);
    }
    if (mode == 6 || mode >= 8) {
        int cb = sceHttpsSetSslCallback(tmpl, (void *)pms_ssl_accept_cb, NULL);
        if (err && errsz) snprintf(err, errsz, "sslcb=0x%x", (unsigned)cb);
    }
    if (mode == 4) {
        /* 3 = TLS 1.2 in the SCE enumeration. GitHub refuses anything older, so a default that
           negotiates TLS 1.0 would fail in exactly the place this does. */
        int mv = sceHttpsSetMinSslVersion(tmpl, 3);
        int sv = sceHttpsSetSslVersion(tmpl, 3);
        if (err && errsz) snprintf(err, errsz, "minver=0x%x setver=0x%x",
                                   (unsigned)mv, (unsigned)sv);
    }
    sceHttpSetConnectTimeOut(tmpl, 20 * 1000 * 1000);
    sceHttpSetRecvTimeOut(tmpl, 30 * 1000 * 1000);
    conn = sceHttpCreateConnectionWithURL(tmpl, url, 1);
    if (conn < 0) PMS_HTTP_FAIL("connect", conn);
    req = sceHttpCreateRequestWithURL(conn, 0 /* GET */, url, 0);
    if (req < 0) PMS_HTTP_FAIL("request", req);
    rc = sceHttpSendRequest(req, NULL, 0);
    if (rc < 0) PMS_HTTP_FAIL("send", rc);
    rc = sceHttpGetStatusCode(req, &status);
    if (rc < 0) PMS_HTTP_FAIL("status", rc);
    for (;;) {
        char buf[8192];
        int n = sceHttpReadData(req, buf, sizeof(buf));
        if (n < 0) PMS_HTTP_FAIL("read", n);
        if (n == 0) break;
        if (out && (size_t)total < outsz) {
            size_t room = outsz - (size_t)total - 1;
            size_t take = ((size_t)n < room) ? (size_t)n : room;
            memcpy(out + total, buf, take);
        }
        total += n;
        if (total > 64LL * 1024 * 1024) break;      /* a probe, not a downloader */
    }
    if (got) *got = total;
    if (out && outsz) out[(size_t)total < outsz ? (size_t)total : outsz - 1] = 0;
#undef PMS_HTTP_FAIL
done:
    snprintf(g_https_ids, sizeof(g_https_ids), "net=%d ssl=%d http=%d tmpl=%d conn=%d req=%d",
             netmem, sslctx, httpctx, tmpl, conn, req);
    if (req >= 0) sceHttpDeleteRequest(req);
    if (conn >= 0) sceHttpDeleteConnection(conn);
    if (tmpl >= 0) sceHttpDeleteTemplate(tmpl);
    if (httpctx >= 0) sceHttpTerm(httpctx);
    if (sslctx >= 0) sceSslTerm(sslctx);
    if (netmem >= 0) sceNetPoolDestroy(netmem);
    return status;
}


static int diagnostics_allowed(void) {
    struct stat st;
    return stat(DIAG_FLAG, &st) == 0;
}
static int pkgserve_register(const char *path);
static const char *strcasestr_local(const char *hay, const char *needle);
static int port_open(int port);

/* ---------------- serving a local package to the install daemon -----------
 * A package already on the console still has to reach the installer the SAME way a
 * downloaded one does: the DPI daemon fetches it over HTTP and installs it. That is
 * the pipeline that produces a playable game.
 *
 * sceAppInstUtilAppInstallPkg is NOT an alternative — it only registers metadata and
 * leaves a tile that fails with "Cannot start the game". Using it for USB installs is
 * exactly what produced a broken Star Wars install.
 *
 * The daemon requires byte ranges, so this answers HEAD, 200 and 206 properly. It runs
 * on its own thread: a 600 MB transfer through the single accept loop would freeze the
 * whole app (UI and cheat engine included) for the duration.
 * ------------------------------------------------------------------------- */

/* Percent-decode a path segment (same rules qparam uses, minus the '+' -> space,
   which would corrupt real filenames containing a plus). */
static void url_decode(const char *in, char *out, size_t outsz) {
    size_t j = 0;
    for (const char *p = in; *p && j < outsz - 1; ) {
        if (p[0] == '%' && hexval(p[1]) >= 0 && hexval(p[2]) >= 0) {
            out[j++] = (char)((hexval(p[1]) << 4) | hexval(p[2]));
            p += 3;
        } else out[j++] = *p++;
    }
    out[j] = 0;
}

/* Short opaque handles for packages we serve to the install daemon. The daemon
   cannot fetch a URL containing percent-escapes, and real package names are full
   of spaces and brackets - so the daemon only ever sees /pkgfile/<n>.pkg.
   The PC path never hit this because it serves /library/<key> with an opaque key. */
#define PKGSERVE_SLOTS 8
typedef struct { int id; char path[700]; } pkgserve_slot_t;
static pkgserve_slot_t g_pkgserve[PKGSERVE_SLOTS];
static int g_pkgserve_next = 1;
static pthread_mutex_t g_pkgserve_lock = PTHREAD_MUTEX_INITIALIZER;

static int pkgserve_register(const char *path) {
    pthread_mutex_lock(&g_pkgserve_lock);
    int id = g_pkgserve_next++;
    pkgserve_slot_t *sl = &g_pkgserve[id % PKGSERVE_SLOTS];
    sl->id = id;
    snprintf(sl->path, sizeof(sl->path), "%s", path);
    pthread_mutex_unlock(&g_pkgserve_lock);
    return id;
}

static int pkgserve_lookup(int id, char *out, size_t outsz) {
    int ok = 0;
    pthread_mutex_lock(&g_pkgserve_lock);
    pkgserve_slot_t *sl = &g_pkgserve[id % PKGSERVE_SLOTS];
    if (id > 0 && sl->id == id && sl->path[0]) { snprintf(out, outsz, "%s", sl->path); ok = 1; }
    pthread_mutex_unlock(&g_pkgserve_lock);
    return ok;
}

/* Only ever serve real package/backup files from storage we actually scan. */
static int pkgfile_path_allowed(const char *p) {
    if (!p || p[0] != '/' || strstr(p, "..")) return 0;
    static const char *ROOTS[] = { "/mnt/usb", "/mnt/ext", "/data/", "/user/data/", NULL };
    int rooted = 0;
    for (int i = 0; ROOTS[i]; i++) if (!strncmp(p, ROOTS[i], strlen(ROOTS[i]))) { rooted = 1; break; }
    if (!rooted) return 0;
    return path_ext_is(p, ".pkg") || is_backup_ext(p);
}

typedef struct { int fd; char path[700]; long long from; int head_only; } pkgserve_t;

static void *pkgfile_thread(void *arg) {
    pkgserve_t *j = (pkgserve_t *)arg;
    {
        /* THE SEND TIMEOUT COMES OFF, exactly as it does on the PS4. The accept loop arms a 30 s
           SO_SNDTIMEO on every socket it accepts (a dead client must not pin it) and then hands
           this worker that same descriptor. For a package stream that is wrong: the install daemon
           stops reading for as long as it takes to promote what it has already taken, which is
           routinely longer than thirty seconds on a large title, and a write that gives up part
           way through is an install that fails with nothing on screen to explain it.
           TCP_NODELAY as well: this socket writes a header and then a body, and without it the
           body can wait on a delayed ACK before the first bytes move. */
        struct timeval none;
        none.tv_sec = 0; none.tv_usec = 0;
        setsockopt(j->fd, SOL_SOCKET, SO_SNDTIMEO, &none, sizeof(none));
        int one = 1;
        setsockopt(j->fd, IPPROTO_TCP, TCP_NODELAY, &one, sizeof(one));
    }
    int in = open(j->path, O_RDONLY);
    if (in < 0) {
        send_status(j->fd, "404 Not Found", "text/plain", "no such package");
        close(j->fd); free(j); return NULL;
    }
    struct stat st;
    /* fstat's result was discarded. On the rare failure - the fd revoked, a filesystem error after
       a successful open - `st` was an uninitialised stack struct, so `total` was garbage and the
       install daemon got a bogus Content-Length over whatever read() produced. Say 500 instead. */
    if (fstat(in, &st) != 0 || !S_ISREG(st.st_mode)) {
        close(in);
        send_status(j->fd, "500 Internal Server Error", "text/plain", "cannot read that package");
        close(j->fd); free(j); return NULL;
    }
    long long total = (long long)st.st_size;
    long long from = j->from;
    if (from < 0 || from >= total) from = 0;
    long long len = total - from;

    char hdr[420];
    int n;
    if (from > 0) {
        n = snprintf(hdr, sizeof(hdr),
                     "HTTP/1.1 206 Partial Content\r\nContent-Type: application/octet-stream\r\n"
                     "Accept-Ranges: bytes\r\nContent-Length: %lld\r\n"
                     "Content-Range: bytes %lld-%lld/%lld\r\nConnection: close\r\n\r\n",
                     len, from, total - 1, total);
    } else {
        n = snprintf(hdr, sizeof(hdr),
                     "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                     "Accept-Ranges: bytes\r\nContent-Length: %lld\r\nConnection: close\r\n\r\n",
                     len);
    }
    write_all(j->fd, hdr, (size_t)n);

    if (!j->head_only) {
        if (from > 0) lseek(in, (off_t)from, SEEK_SET);
        char *buf = (char *)malloc(COPY_BUF_BYTES);   /* heap: worker stacks are tiny */
        if (buf) {
            ssize_t r;
            while ((r = read(in, buf, COPY_BUF_BYTES)) > 0) {
                size_t off = 0;
                while (off < (size_t)r) {
                    ssize_t w = write(j->fd, buf + off, (size_t)r - off);
                    if (w <= 0) { r = -1; break; }
                    off += (size_t)w;
                }
                if (r < 0) break;                     /* client went away mid-transfer */
            }
            free(buf);
        }
    }
    close(in);
    close(j->fd);
    free(j);
    return NULL;
}

/* Returns 0 when the request was taken over (socket now owned by the worker). */
static int pkgfile_serve(int fd, const char *rawpath, const char *raw_request) {
    /* Keep the leading slash: the URL is /pkgfile + the absolute path, so the file
       path starts at "/pkgfile" (8 chars), not past the following slash. */
    const char *enc = rawpath + strlen("/pkgfile");
    char path[700];
    url_decode(enc, path, sizeof(path));
    char *q = strchr(path, '?'); if (q) *q = 0;
    /* /pkgfile/<n>.pkg -> a package registered for the install daemon. */
    if (path[0] == '/' && path[1] >= '1' && path[1] <= '9') {
        const char *dot = strchr(path + 1, '.');
        const char *stop = dot ? dot : path + strlen(path);
        int alldigits = 1;
        for (const char *c = path + 1; c < stop; c++)
            if (*c < '0' || *c > '9') { alldigits = 0; break; }
        if (alldigits) {
            char real[700];
            if (pkgserve_lookup(atoi(path + 1), real, sizeof(real)))
                snprintf(path, sizeof(path), "%s", real);
        }
    }
    if (!pkgfile_path_allowed(path)) {
        send_status(fd, "403 Forbidden", "text/plain", "not a servable package path");
        return -1;
    }
    struct stat st;
    if (stat(path, &st) != 0 || !S_ISREG(st.st_mode)) {
        send_status(fd, "404 Not Found", "text/plain", "no such package");
        return -1;
    }
    long long from = 0;
    const char *rh = raw_request ? strcasestr_local(raw_request, "\nrange:") : NULL;
    if (rh) {
        const char *eq = strchr(rh, '=');
        if (eq) from = strtoll(eq + 1, NULL, 10);
    }
    pkgserve_t *j = (pkgserve_t *)calloc(1, sizeof(*j));
    if (!j) { send_status(fd, "500 Internal Server Error", "text/plain", "oom"); return -1; }
    j->fd = fd;
    j->from = from;
    j->head_only = raw_request && !strncmp(raw_request, "HEAD ", 5);
    snprintf(j->path, sizeof(j->path), "%s", path);

    pthread_t t;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 256 * 1024);
    int rc = pthread_create(&t, &attr, pkgfile_thread, j);
    pthread_attr_destroy(&attr);
    if (rc != 0) { free(j); send_status(fd, "503 Service Unavailable", "text/plain", "busy"); return -1; }
    pthread_detach(t);
    return 0;                                        /* worker owns the socket now */
}

/* The console's own DPI client lived here: dpi_host_kind(), which probed :12800 to find out
   whether Elf Arsenal or etaHEN owned it, and dpi_install_url(), which then spoke that
   daemon's protocol. dpi_install_url has been unreachable since the console-local lane moved
   to our own spawned installer - the compiler has flagged it unused on every build since -
   and there is no daemon on 12800 to identify any more. Both are gone. */

/* Run an install through OUR OWN engine and wait for its verdict.

   Writes the request, has Payload Manager spawn pms-installer.elf, then polls for the result file.
   Blocking, so callers use it from a worker thread. Returns 0 on success, negative otherwise, and
   fills `reply` with something a human can read.

   This is the same lane /api/engine/install-spawn drives; having it as a function is what lets the
   console-local (USB) route stop talking to a third-party daemon on :12800. */
/* Write pms-installer.elf out of our own .rodata into Payload Manager's payload directory.
   Done every time rather than trusting payload_bootstrap: that thread waits out the
   user-configurable autostart delay, so on a fresh boot the file is simply not there yet. Writing
   it each time also guarantees the installer always matches this build. */
/* Lay the spawned installer down where Payload Manager will find it.
   Returns 0 only when the FIRST destination - the one pldmgr executes - is provably complete.

   Every step here exists because the previous version had none of them: it ignored a failed
   open(), abandoned the file on a short write, never fsynced, never checked the size, and returned
   void. The result was a partially written ELF handed straight to elfldr with pm_get still
   reporting success, which produces exactly "installer spawned rc=0" followed by no verdict. */
static int spawn_installer_write(void) {
    mkdir("/data/pldmgr", 0777);
    mkdir("/data/pldmgr/payloads", 0777);
    mkdir("/data/pldmgr/payloads/pms-installer", 0777);
    mkdir(SHOP_DATA_DIR "/payloads", 0777);
    const char *dests[2] = { "/data/pldmgr/payloads/pms-installer/pms-installer.elf",
                             SHOP_DATA_DIR "/payloads/pms-installer.elf" };
    const unsigned int total = (unsigned int)(pb_installer_end - pb_installer);
    int first_ok = -1;

    for (int d = 0; d < 2; d++) {
        /* Write beside it and rename, so the live file is replaced atomically and pldmgr can never
           execute a half-written image - not even if we are killed mid-write. */
        char tmp[300];
        snprintf(tmp, sizeof(tmp), "%s.part", dests[d]);
        unlink(tmp);
        int dst = open(tmp, O_WRONLY | O_CREAT | O_TRUNC, 0777);
        if (dst < 0) {
            ilog("install: cannot open %s (errno %d)", tmp, errno);
            if (d == 0) first_ok = -2;
            continue;
        }
        const unsigned char *ib = pb_installer;
        unsigned int left = total;
        int bad = 0;
        while (left) {
            ssize_t w = write(dst, ib, left > 65536 ? 65536 : left);
            if (w <= 0) { bad = 1; break; }        /* ENOSPC, EIO - do NOT keep the file */
            ib += w;
            left -= (unsigned int)w;
        }
        if (!bad) fsync(dst);                      /* on disk before anything executes it */
        close(dst);

        struct stat stb;
        if (bad || left || stat(tmp, &stb) != 0 || (unsigned int)stb.st_size != total) {
            ilog("install: installer write FAILED for %s (%u of %u bytes)",
                 dests[d], total - left, total);
            unlink(tmp);
            if (d == 0) first_ok = -3;
            continue;
        }
        if (rename(tmp, dests[d]) != 0) {
            ilog("install: could not put the installer in place at %s (errno %d)", dests[d], errno);
            unlink(tmp);
            if (d == 0) first_ok = -4;
            continue;
        }
        if (d == 0) first_ok = 0;
    }
    return first_ok;
}

/* Take the installer back out of Payload Manager's folder.
 *
 * It only needs to exist there for the moment pldmgr spawns it. Leaving it makes our own engine
 * show up in the user's launcher as though it were a separate payload they installed, which is
 * exactly what they asked us not to do - everything of ours belongs inside our app.
 *
 * Safe while the installer is still running: unlinking an executable does not disturb a process
 * already started from it. And spawn_installer_write() rebuilds it before every spawn, from the
 * bytes embedded in this ELF, so nothing depends on it persisting between installs. */
static void spawn_installer_remove(void) {
    unlink("/data/pldmgr/payloads/pms-installer/pms-installer.elf");
    unlink("/data/pldmgr/payloads/pms-installer/pms-installer.elf.json");
    /* The directory too, or Payload Manager still lists an empty entry. */
    rmdir("/data/pldmgr/payloads/pms-installer");
}

/* `reply` is always a finished sentence fit for the television - no code in it. The console's
   own error code, when there is one, comes back through *code_out so the API reply and the log can
   still quote it; the house style keeps hex off the screen. */
static int spawn_install_wait(const char *uri, const char *label, char *reply, size_t rsz,
                              unsigned *code_out) {
    if (code_out) *code_out = 0;
    long long token = 0;
    if (spawn_lane_claim(&token) != 0) {
        snprintf(reply, rsz, "Another install is still being handed over - it will start "
                             "as soon as that one is accepted");
        return -10;
    }
    ilog("install: requested  uri=%s  label=%s  token=%lld",
         uri, label && label[0] ? label : "-", token);

    mkdir(SHOP_DATA_DIR, 0777);
    unlink(SPAWN_RES_PATH);
    int rq = open(SPAWN_REQ_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (rq < 0) {
        spawn_lane_release();
        snprintf(reply, rsz, "The console would not let the shop write to its own data folder - "
                             "load the shop again from Payload Manager");
        return -11;
    }
    {
        /* Line 3 is the token the installer echoes back - see g_spawn_token. */
        char line[1750];
        int ln = snprintf(line, sizeof(line), "%s\n%s\n%lld\n", uri, label ? label : "", token);
        if (ln < 0) ln = 0;
        if ((size_t)ln > sizeof(line)) ln = (int)sizeof(line);
        ssize_t w = write(rq, line, (size_t)ln);
        (void)w;
    }
    close(rq);

    /* Refuse rather than spawn an installer we cannot vouch for. The blocking lane checks this
       for the same reason the async one does: pldmgr executes whatever is on disk, and a short
       write there is the one failure with no upper bound on its consequences. */
    if (spawn_installer_write() != 0) {
        unlink(SPAWN_REQ_PATH);       /* a request nobody is going to run must not outlive this */
        spawn_lane_release();
        ilog("install: ABORTED - the installer could not be written intact");
        snprintf(reply, rsz, "The console would not accept our installer onto its own storage - "
                             "it may be out of space. Nothing was started.");
        return -15;
    }
    if (pm_get("/loadpayload:/data/pldmgr/payloads/pms-installer/pms-installer.elf") != 0) {
        /* Nothing was spawned, so nothing of ours belongs in the launcher or the data folder.
           Leaving the request behind meant a later hand-launched installer re-ran THIS package
           onto whatever was live by then - the duplicate-install shape. */
        unlink(SPAWN_REQ_PATH);
        spawn_installer_remove();
        spawn_lane_release();
        ilog("install: FAILED - Payload Manager would not spawn the installer");
        snprintf(reply, rsz, "Payload Manager did not answer on :8084 - reload it on the PS5, "
                             "then try this install again");
        return -12;
    }

    /* The installer makes one call and exits, so this is quick - but a busy console can take a
       few seconds to schedule it. */
    int said_stale = 0;
    for (int i = 0; i < 120; i++) {
        usleep(500000);
        int rf = open(SPAWN_RES_PATH, O_RDONLY);
        if (rf < 0) continue;
        /* Sized past the installer's own out[1900]: the token is the LAST field of the verdict,
           and a 1200-byte read cut it off behind a long PC /library/ URL, so this lane judged its
           own verdict stale and timed out on an install the console had accepted. */
        char buf[2048] = {0};
        ssize_t got = read(rf, buf, sizeof(buf) - 1);
        close(rf);
        if (got <= 0) continue;
        buf[got] = 0;
        if (!spawn_verdict_is_ours(buf, token)) {
            /* A verdict from an EARLIER installer that finished late. Not this job's - keep
               waiting for the one we spawned, which overwrites the file when it is done. */
            if (!said_stale) { said_stale = 1; ilog("install: ignoring a stale verdict  %s", buf); }
            continue;
        }
        unlink(SPAWN_REQ_PATH);       /* consumed: the request must never be runnable twice */
        spawn_lane_release();
        spawn_installer_remove();     /* this lane collects its own verdict - clean up here too */
        ilog("install: installer replied  %s", buf);
        if (strstr(buf, "\"ok\":true")) {
            snprintf(reply, rsz, "%s", "The console accepted it and is downloading it now");
            ilog("install: ACCEPTED after %llds", (long long)(time(NULL) - g_spawn_started));
            return 0;
        }
        /* The eight digits, and only the eight digits. rcp+6 lands inside a JSON document with
           no terminator of its own, so printing it as a string pasted the entire rest of the
           reply - authid, pid, uri and all - into the sentence shown on the television. */
        const char *rcp = strstr(buf, "\"rc\":\"0x");
        unsigned code = 0;
        if (rcp) code = (unsigned)strtoul(rcp + 8, NULL, 16);
        if (code_out) *code_out = code;
        /* Words only. The bracketed hex used to ride along here and from here onto the
           television; the code still reaches install.log (just below) and the API reply. */
        if (code)
            snprintf(reply, rsz, "%s", install_error_text(code));
        else
            snprintf(reply, rsz, "%s", "The console refused it and did not say why");
        ilog("install: REFUSED  rc=0x%08X  %s", code, buf);
        return -13;
    }
    unlink(SPAWN_REQ_PATH);           /* no verdict, but the request is spent either way */
    spawn_lane_release();
    ilog("install: TIMEOUT - the installer never wrote a result");
    snprintf(reply, rsz, "The installer never reported back - the console may be busy. Try "
                         "again, and reload the shop from Payload Manager if it keeps happening");
    return -14;
}

/* A local (USB / already-on-console) install in flight. It runs on its own thread and the UI
   polls the state. */
typedef struct {
    int   active, done, ok, held;
    /* etaHEN answers "SUCCESS: Task started" the moment it has QUEUED the download - the install
       itself then runs for minutes and can still fail (bad dump, no space, a crashed installer
       queue). Reporting that acceptance as done+ok put "Installed" on the queue row, and a PS5
       notification saying so, before a single byte had been fetched. This flag travels to the
       companion, which owns the only honest proof (a completed bgft row / the game app.pkg). */
    int   accepted_only;
    char  name[240], path[700], url[160], msg[400];
} localinst_t;
static localinst_t g_linst;
static pthread_mutex_t g_linst_lock = PTHREAD_MUTEX_INITIALIZER;

static void *localinst_thread(void *arg) {
    (void)arg;
    char url[160], nm[240];
    pthread_mutex_lock(&g_linst_lock);
    snprintf(url, sizeof(url), "%s", g_linst.url);
    snprintf(nm, sizeof(nm), "%s", g_linst.name);
    pthread_mutex_unlock(&g_linst_lock);

    char reply[600] = {0};
    unsigned code = 0;
    /* OUR engine. This used to POST to whatever daemon owned :12800 - that is the last place the
       console-local lane depended on somebody else's software. The verdict is still an ACCEPTANCE,
       not a completion: the console downloads and installs on its own afterwards, and only its own
       records prove that finished. */
    int rc = spawn_install_wait(url, nm, reply, sizeof(reply), &code);

    pthread_mutex_lock(&g_linst_lock);
    g_linst.ok = (rc == 0);
    g_linst.accepted_only = (rc == 0);
    g_linst.done = 1;
    g_linst.active = 0;
    /* The API keeps the console's code in brackets so a bug report can quote it; the toast below
       gets the sentence alone, because a hex code on a television says nothing to anyone. */
    if (rc != 0 && reply[0] && code)
        snprintf(g_linst.msg, sizeof(g_linst.msg), "%s (0x%08X)", reply, code);
    else
        snprintf(g_linst.msg, sizeof(g_linst.msg), "%s",
                 rc != 0 ? (reply[0] ? reply : "the install engine refused the package")
                         : "Accepted - the console is downloading it now");
    pthread_mutex_unlock(&g_linst_lock);

    /* Say what is happening RIGHT NOW. The console posts its own "Ready to play" when the download
       actually finishes; claiming that here would be a lie at hand-off. */
    if (rc == 0)
        notifyf("%s is installing\nThe console is downloading it now - watch your home screen",
                nm[0] ? nm : "Your game");
    else
        /* reply is now always a finished sentence (see spawn_install_wait), so it can go straight
           onto the television. The raw installer JSON stays in install.log, where it belongs. */
        notifyf("%s could not be installed\n%s", nm[0] ? nm : "That package",
                reply[0] ? reply : "The console refused it");
    return NULL;
}

/* These are all defined further down the file; the GitHub lane needs them up here. */
static long json_num_after(const char *json, const char *key, long def);
static int copy_file_simple(const char *src, const char *dst);
static const char *json_str_after_lim(const char *p, const char *end, const char *key,
                                      char *out, size_t outsz);

/* ---------------- SHA-256 ------------------------------------------------
 * The release API gives every asset a `digest` ("sha256:<hex>"), and it arrives from
 * api.github.com while the bytes arrive from release-assets.githubusercontent.com - two
 * different hosts. Checking one against the other catches a truncated, corrupted or substituted
 * file on the asset path, which matters more here than in most places: what this lane downloads
 * gets RUN on the console.
 *
 * It is not a signature and is not presented as one. GitHub states both numbers, so anyone able
 * to rewrite both could state a consistent lie. What it is: proof that the file we stored is the
 * file GitHub described.
 * ------------------------------------------------------------------------- */
typedef struct {
    unsigned int       h[8];
    unsigned long long bits;
    unsigned char      buf[64];
    size_t             n;
} pms_sha256_t;

static const unsigned int PMS_SHA_K[64] = {
    0x428a2f98u,0x71374491u,0xb5c0fbcfu,0xe9b5dba5u,0x3956c25bu,0x59f111f1u,0x923f82a4u,0xab1c5ed5u,
    0xd807aa98u,0x12835b01u,0x243185beu,0x550c7dc3u,0x72be5d74u,0x80deb1feu,0x9bdc06a7u,0xc19bf174u,
    0xe49b69c1u,0xefbe4786u,0x0fc19dc6u,0x240ca1ccu,0x2de92c6fu,0x4a7484aau,0x5cb0a9dcu,0x76f988dau,
    0x983e5152u,0xa831c66du,0xb00327c8u,0xbf597fc7u,0xc6e00bf3u,0xd5a79147u,0x06ca6351u,0x14292967u,
    0x27b70a85u,0x2e1b2138u,0x4d2c6dfcu,0x53380d13u,0x650a7354u,0x766a0abbu,0x81c2c92eu,0x92722c85u,
    0xa2bfe8a1u,0xa81a664bu,0xc24b8b70u,0xc76c51a3u,0xd192e819u,0xd6990624u,0xf40e3585u,0x106aa070u,
    0x19a4c116u,0x1e376c08u,0x2748774cu,0x34b0bcb5u,0x391c0cb3u,0x4ed8aa4au,0x5b9cca4fu,0x682e6ff3u,
    0x748f82eeu,0x78a5636fu,0x84c87814u,0x8cc70208u,0x90befffau,0xa4506cebu,0xbef9a3f7u,0xc67178f2u
};

#define PMS_ROR(x, n) (((x) >> (n)) | ((x) << (32 - (n))))

static void pms_sha256_init(pms_sha256_t *c) {
    c->h[0] = 0x6a09e667u; c->h[1] = 0xbb67ae85u; c->h[2] = 0x3c6ef372u; c->h[3] = 0xa54ff53au;
    c->h[4] = 0x510e527fu; c->h[5] = 0x9b05688cu; c->h[6] = 0x1f83d9abu; c->h[7] = 0x5be0cd19u;
    c->bits = 0;
    c->n = 0;
}

static void pms_sha256_block(pms_sha256_t *c, const unsigned char *p) {
    unsigned int w[64], a, b2, d, e, f, g, hh, cc, t1, t2;
    int i;
    for (i = 0; i < 16; i++)
        w[i] = ((unsigned int)p[i * 4] << 24) | ((unsigned int)p[i * 4 + 1] << 16) |
               ((unsigned int)p[i * 4 + 2] << 8) | (unsigned int)p[i * 4 + 3];
    for (i = 16; i < 64; i++) {
        unsigned int s0 = PMS_ROR(w[i - 15], 7) ^ PMS_ROR(w[i - 15], 18) ^ (w[i - 15] >> 3);
        unsigned int s1 = PMS_ROR(w[i - 2], 17) ^ PMS_ROR(w[i - 2], 19) ^ (w[i - 2] >> 10);
        w[i] = w[i - 16] + s0 + w[i - 7] + s1;
    }
    a = c->h[0]; b2 = c->h[1]; cc = c->h[2]; d = c->h[3];
    e = c->h[4]; f = c->h[5]; g = c->h[6]; hh = c->h[7];
    for (i = 0; i < 64; i++) {
        unsigned int S1 = PMS_ROR(e, 6) ^ PMS_ROR(e, 11) ^ PMS_ROR(e, 25);
        unsigned int ch = (e & f) ^ ((~e) & g);
        unsigned int S0 = PMS_ROR(a, 2) ^ PMS_ROR(a, 13) ^ PMS_ROR(a, 22);
        unsigned int mj = (a & b2) ^ (a & cc) ^ (b2 & cc);
        t1 = hh + S1 + ch + PMS_SHA_K[i] + w[i];
        t2 = S0 + mj;
        hh = g; g = f; f = e; e = d + t1;
        d = cc; cc = b2; b2 = a; a = t1 + t2;
    }
    c->h[0] += a; c->h[1] += b2; c->h[2] += cc; c->h[3] += d;
    c->h[4] += e; c->h[5] += f;  c->h[6] += g;  c->h[7] += hh;
}

static void pms_sha256_update(pms_sha256_t *c, const void *data, size_t len) {
    const unsigned char *p = (const unsigned char *)data;
    c->bits += (unsigned long long)len * 8ULL;
    while (len) {
        size_t room = 64 - c->n;
        size_t take = (len < room) ? len : room;
        memcpy(c->buf + c->n, p, take);
        c->n += take;
        p += take;
        len -= take;
        if (c->n == 64) { pms_sha256_block(c, c->buf); c->n = 0; }
    }
}

static void pms_sha256_final(pms_sha256_t *c, char *hex, size_t hexsz) {
    unsigned long long bits = c->bits;         /* the padding must not change the length */
    unsigned char pad = 0x80;
    int i;
    pms_sha256_update(c, &pad, 1);
    pad = 0;
    while (c->n != 56) pms_sha256_update(c, &pad, 1);
    for (i = 0; i < 8; i++) c->buf[56 + i] = (unsigned char)(bits >> (56 - 8 * i));
    pms_sha256_block(c, c->buf);
    c->n = 0;
    if (hex && hexsz >= 65) {
        static const char *hx = "0123456789abcdef";
        for (i = 0; i < 8; i++) {
            unsigned int v = c->h[i];
            hex[i * 8 + 0] = hx[(v >> 28) & 15]; hex[i * 8 + 1] = hx[(v >> 24) & 15];
            hex[i * 8 + 2] = hx[(v >> 20) & 15]; hex[i * 8 + 3] = hx[(v >> 16) & 15];
            hex[i * 8 + 4] = hx[(v >> 12) & 15]; hex[i * 8 + 5] = hx[(v >> 8) & 15];
            hex[i * 8 + 6] = hx[(v >> 4) & 15];  hex[i * 8 + 7] = hx[v & 15];
        }
        hex[64] = 0;
    }
}
#undef PMS_ROR

/* Hex sha256 of a file. Returns 0 on success. */
static int sha256_file(const char *path, char *hex, size_t hexsz) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    char *buf = (char *)malloc(256 * 1024);
    if (!buf) { close(fd); return -1; }
    pms_sha256_t c;
    pms_sha256_init(&c);
    for (;;) {
        ssize_t r = read(fd, buf, 256 * 1024);
        if (r < 0) {
            if (errno == EINTR) continue;
            free(buf);
            close(fd);
            return -1;
        }
        if (r == 0) break;
        pms_sha256_update(&c, buf, (size_t)r);
    }
    free(buf);
    close(fd);
    pms_sha256_final(&c, hex, hexsz);
    return 0;
}

/* ---------------- which HTTPS hosts this console can actually reach -------------------------
   MEASURED on the live PS5 (firmware 13.60) through the gated /api/net/https probe, one request
   per host, with the SSL callback installed:

     https://example.com/                           200   fine
     https://api.github.com/                        200   fine
     https://raw.githubusercontent.com/...          200   fine
     https://objects.githubusercontent.com/         404   fine (404 is the right answer to "/")
     https://release-assets.githubusercontent.com/  404   fine
     https://github.com/                             -1   send=0x80431073
     https://codeload.github.com/                    -1   send=0x80431073

   So the earlier conclusion that "GitHub on-console is blocked by TLS" was wrong in both
   directions: TLS works nearly everywhere, and it fails on exactly two hosts. Those two are the
   ones presenting an ECDSA P-384 chain (github.com and codeload.github.com chain to Sectigo
   Public Server Authentication Root E46), while every host that works chains to ISRG or DigiCert
   RSA. It fails inside the net layer, before the verification callback is consulted - which is
   why the code is 0x8043xxxx and not an SSL error.

   It matters because `browser_download_url` is ALWAYS on github.com. The route to the same bytes
   is the asset's API url with `Accept: application/octet-stream`, which answers 302 to
   release-assets.githubusercontent.com. That is what release_assets() records.
   ------------------------------------------------------------------------------------------- */
static int host_unreachable_here(const char *url) {
    return !strncmp(url, "https://github.com/", 19) ||
           !strncmp(url, "https://codeload.github.com/", 28);
}

/* ---- what the host does with a download's progress --------------------------------------
   https_download is shared with the PS4 build (tools/ps4_sync_github_lane.py), and the two
   servers track a transfer differently: this one has a full job record with install fields, the
   PS4 has a smaller one. Two hooks instead of a shared struct, so the shared code does not have
   to know. */
static void pms_dl_progress(long long done, long long total) {
    pthread_mutex_lock(&g_job_mtx);
    g_job.done = done;
    if (total >= 0) g_job.total = total;
    pthread_mutex_unlock(&g_job_mtx);
}

static int pms_dl_cancelled(void) {
    pthread_mutex_lock(&g_job_mtx);
    int c = g_job.cancel;
    pthread_mutex_unlock(&g_job_mtx);
    return c;
}

/* The two questions the shared same-stem cleanup asks this console: what a payload file's
   comparable stem is, and whether this build carries that exact file. */
static void pms_file_stem(const char *name, char *out, size_t outsz) {
    pm_stem(name, out, outsz);
}

static int pms_name_is_bundled(const char *name) {
    for (int i = 0; i < PAYLOAD_BUNDLE_COUNT; i++)
        if (!strcmp(PAYLOAD_BUNDLE[i].filename, name)) return 1;
    return 0;
}

/* How many bytes the host is expecting in total, or 0 when it does not know - the short-transfer
   check compares against it. */
static long long pms_dl_expected(void) {
    pthread_mutex_lock(&g_job_mtx);
    long long t = g_job.total;
    pthread_mutex_unlock(&g_job_mtx);
    return t;
}

/* The shipping HTTPS read: one small GET, into a caller's buffer.

   NOT https_get_mode. That is an eight-mode instrument behind the diagnostics flag, and the
   shipping path was one of its branches - which meant the thing the app depends on could only be
   read by reading the thing that exists to vary it, and the PS4 cannot compile half of it (module
   loading, certificate stores). One implementation ships; the probe stays a probe.

   `*got` is the body's TOTAL length even when it did not all fit, so a caller can tell a
   truncated read from a short document. */
static int https_get_text(const char *url, char *out, size_t outsz, long long *got,
                          char *err, size_t errsz) {
    int netmem = -1, sslctx = -1, httpctx = -1, tmpl = -1, conn = -1, req = -1;
    int status = -1, rc;
    long long total = 0;
    char *buf = NULL;
    if (err && errsz) err[0] = 0;
    if (out && outsz) out[0] = 0;
    if (got) *got = 0;

#define PMS_HG_FAIL(what, code) do { \
        if (err && errsz) snprintf(err, errsz, "%s=0x%x", (what), (unsigned)(code)); \
        goto done; } while (0)

    if (host_unreachable_here(url)) {
        if (err && errsz) snprintf(err, errsz, "this console cannot reach that host");
        return -1;
    }
    netmem = sceNetPoolCreate("pms-get", 64 * 1024, 0);
    if (netmem < 0) PMS_HG_FAIL("netpool", netmem);
    sslctx = sceSslInit(512 * 1024);
    if (sslctx < 0) PMS_HG_FAIL("sslinit", sslctx);
    httpctx = sceHttpInit(netmem, sslctx, 512 * 1024);
    if (httpctx < 0) PMS_HG_FAIL("httpinit", httpctx);
    tmpl = sceHttpCreateTemplate(httpctx, "PKG-MUTANT-SHOP", 1, 1);
    if (tmpl < 0) PMS_HG_FAIL("template", tmpl);
    sceHttpSetAutoRedirect(tmpl, 1);
    /* THE ONE CALL THAT MAKES TLS WORK HERE - see the note beside pms_ssl_accept_cb. */
    sceHttpsSetSslCallback(tmpl, (void *)pms_ssl_accept_cb, NULL);
    sceHttpSetConnectTimeOut(tmpl, 20 * 1000 * 1000);
    sceHttpSetSendTimeOut(tmpl, 20 * 1000 * 1000);
    sceHttpSetRecvTimeOut(tmpl, 30 * 1000 * 1000);
    conn = sceHttpCreateConnectionWithURL(tmpl, url, 1);
    if (conn < 0) PMS_HG_FAIL("connect", conn);
    req = sceHttpCreateRequestWithURL(conn, 0, url, 0);
    if (req < 0) PMS_HG_FAIL("request", req);
    /* GitHub answers 403 to a request with no User-Agent; the template sets one. Asking for the
       documented media type keeps the shape of the reply stable. */
    sceHttpAddRequestHeader(req, "Accept", "application/vnd.github+json", 0);
    rc = sceHttpSendRequest(req, NULL, 0);
    if (rc < 0) PMS_HG_FAIL("send", rc);
    rc = sceHttpGetStatusCode(req, &status);
    if (rc < 0) PMS_HG_FAIL("status", rc);
    buf = (char *)malloc(64 * 1024);
    if (!buf) { if (err && errsz) snprintf(err, errsz, "out of memory"); goto done; }
    for (;;) {
        int n = sceHttpReadData(req, buf, 64 * 1024);
        if (n == 0) break;
        if (n < 0) PMS_HG_FAIL("read", n);
        if (out && (size_t)total < outsz) {
            size_t room = outsz - (size_t)total - 1;
            size_t take = ((size_t)n < room) ? (size_t)n : room;
            memcpy(out + total, buf, take);
        }
        total += n;
        /* A release document is tens of kilobytes. Anything past this is not one, and reading it
           into a console's memory is not something to do politely. */
        if (total > 4LL * 1024 * 1024) break;
    }
    if (out && outsz) out[(size_t)total < outsz ? (size_t)total : outsz - 1] = 0;
#undef PMS_HG_FAIL
done:
    if (got) *got = total;
    if (buf) free(buf);
    if (req >= 0) sceHttpDeleteRequest(req);
    if (conn >= 0) sceHttpDeleteConnection(conn);
    if (tmpl >= 0) sceHttpDeleteTemplate(tmpl);
    if (httpctx >= 0) sceHttpTerm(httpctx);
    if (sslctx >= 0) sceSslTerm(sslctx);
    if (netmem >= 0) sceNetPoolDestroy(netmem);
    return status;
}

/* Download over TLS, to http_download()'s contract.

   Deliberately the same shape as http_download: `dest` is written directly (the caller owns the
   .part naming and the rename), progress and cancellation go through g_job, and the return is 0,
   -1 for a failure or -2 for a cancellation. That is what lets one job record, one progress bar
   and one Cancel button serve both transports - the first version of this was a second job
   system beside the one that already worked.

   Resume works the same way and for the same reason: a 268 MB emulator that dropped at 80% must
   not start again from zero. */
static int https_download(const char *url, const char *dest, char *err, size_t errsz) {
    int netmem = -1, sslctx = -1, httpctx = -1, tmpl = -1, conn = -1, req = -1;
    int status = 0, rc, fd = -1, ret = -1;
    long long have = 0, clen = -1;
    char *buf = NULL;
    struct stat stt;
    if (err && errsz) err[0] = 0;

#define PMS_HD_FAIL(what, code) do { \
        if (err && errsz) snprintf(err, errsz, "%s=0x%x", (what), (unsigned)(code)); \
        ret = -1; goto done; } while (0)

    if (host_unreachable_here(url)) {
        if (err && errsz)
            snprintf(err, errsz, "this console cannot reach that host - the release API's asset "
                                 "link has to be used instead");
        return -1;
    }

    /* What is already on disk, if anything. */
    if (stat(dest, &stt) == 0 && stt.st_size > 0) have = (long long)stt.st_size;

    netmem = sceNetPoolCreate("pms-dl", 256 * 1024, 0);
    if (netmem < 0) PMS_HD_FAIL("netpool", netmem);
    sslctx = sceSslInit(1024 * 1024);
    if (sslctx < 0) PMS_HD_FAIL("sslinit", sslctx);
    httpctx = sceHttpInit(netmem, sslctx, 1024 * 1024);
    if (httpctx < 0) PMS_HD_FAIL("httpinit", httpctx);
    tmpl = sceHttpCreateTemplate(httpctx, "PKG-MUTANT-SHOP", 1, 1);
    if (tmpl < 0) PMS_HD_FAIL("template", tmpl);
    /* An asset's API url answers 302 to the CDN, so redirects are not optional. */
    sceHttpSetAutoRedirect(tmpl, 1);
    /* THE ONE CALL THAT MAKES TLS WORK HERE - see the note beside pms_ssl_accept_cb. */
    sceHttpsSetSslCallback(tmpl, (void *)pms_ssl_accept_cb, NULL);
    sceHttpSetConnectTimeOut(tmpl, 30 * 1000 * 1000);
    sceHttpSetSendTimeOut(tmpl, 30 * 1000 * 1000);
    /* Per read, not per transfer: a 2 GB package legitimately takes many minutes. */
    sceHttpSetRecvTimeOut(tmpl, 60 * 1000 * 1000);
    conn = sceHttpCreateConnectionWithURL(tmpl, url, 1);
    if (conn < 0) PMS_HD_FAIL("connect", conn);
    req = sceHttpCreateRequestWithURL(conn, 0, url, 0);
    if (req < 0) PMS_HD_FAIL("request", req);
    /* THE BYTES, NOT THE DESCRIPTION. An asset's API url answers with the asset's JSON unless
       this header is present. Harmless for every other kind of URL this lane is handed. */
    sceHttpAddRequestHeader(req, "Accept", "application/octet-stream", 0);
    if (have > 0) {
        char rv[64];
        snprintf(rv, sizeof(rv), "bytes=%lld-", have);
        sceHttpAddRequestHeader(req, "Range", rv, 0);      /* 0 = overwrite, not append */
    }
    rc = sceHttpSendRequest(req, NULL, 0);
    if (rc < 0) PMS_HD_FAIL("send", rc);
    rc = sceHttpGetStatusCode(req, &status);
    if (rc < 0) PMS_HD_FAIL("status", rc);
    if (status == 416 && have > 0) {
        /* Asked for bytes past the end: the whole file is already here. Not a failure - the same
           case the plain-HTTP lane learned to treat as success. */
        pms_dl_progress(have, have);
        ret = 0;
        goto done;
    }
    if (status != 200 && status != 206) {
        if (err && errsz) snprintf(err, errsz, "the server answered HTTP %d", status);
        ret = -1;
        goto done;
    }
    /* A 200 in answer to a Range request means the server ignored it, so what is on disk is not
       a prefix of what is arriving and must not be appended to. */
    if (have > 0 && status == 200) have = 0;

    {
        int flag = 0;
        unsigned long long cl64 = 0;
        if (sceHttpGetResponseContentLength(req, &flag, &cl64) >= 0 && cl64 > 0)
            clen = (long long)cl64;
    }
    pms_dl_progress(have, (clen > 0) ? have + clen : 0);

    mkparents(dest);
    fd = open(dest, O_WRONLY | O_CREAT | (have > 0 ? O_APPEND : O_TRUNC), 0777);
    if (fd < 0) PMS_HD_FAIL("open", errno);

    /* Heap, not stack: this runs on a worker thread with a bounded stack, and DL_BUF is the
       measured knee for read/write throughput on this kernel. */
    buf = (char *)malloc(DL_BUF);
    if (!buf) { if (err && errsz) snprintf(err, errsz, "out of memory"); ret = -1; goto done; }
    for (;;) {
        pms_dl_progress(have, -1);
        if (pms_dl_cancelled()) {
            if (err && errsz) snprintf(err, errsz, "canceled");
            ret = -2;
            goto done;
        }
        int n = sceHttpReadData(req, buf, DL_BUF);
        if (n == 0) break;                                   /* the body ended */
        if (n < 0) {
            if (err && errsz)
                snprintf(err, errsz, "the connection dropped at %lld bytes (0x%x)", have,
                         (unsigned)n);
            ret = -1;
            goto done;
        }
        if (write_all_checked(fd, buf, (size_t)n) != (ssize_t)n) {
            /* Do NOT keep counting. Advancing past bytes that were never stored is how a
               disk-full transfer used to finish "complete" and install a truncated package. */
            if (err && errsz)
                snprintf(err, errsz, "could not write to storage at %lld bytes (disk full?)",
                         have);
            ret = -1;
            goto done;
        }
        have += n;
    }
    close(fd);
    fd = -1;
    {
        long long want = pms_dl_expected();
        pms_dl_progress(have, -1);
        if (want > 0 && have < want) {
            if (err && errsz)
                snprintf(err, errsz, "short transfer: got %lld of %lld bytes", have, want);
            ret = -1;
            goto done;
        }
    }
    ret = 0;
#undef PMS_HD_FAIL
done:
    if (fd >= 0) close(fd);
    if (buf) free(buf);
    if (req >= 0) sceHttpDeleteRequest(req);
    if (conn >= 0) sceHttpDeleteConnection(conn);
    if (tmpl >= 0) sceHttpDeleteTemplate(tmpl);
    if (httpctx >= 0) sceHttpTerm(httpctx);
    if (sslctx >= 0) sceSslTerm(sslctx);
    if (netmem >= 0) sceNetPoolDestroy(netmem);
    return ret;
}

/* ONE VERSION OF A PAYLOAD IN A FOLDER, NEVER TWO. A new release usually has a new file name
   (..._v0.6.1.elf beside ..._v0.5.0.elf) and both would then look like the same project, so the
   previous copy goes as the new one lands - the same thing the companion's download_asset
   refuses to leave behind.

   A FILE THIS BUILD CARRIES IS NEVER REMOVED. Measured on the PS5, whose payload folder held
   webkit-autoloader-installer.elf (written there by the ELF, under the stable catalogue name)
   beside webkit-autoloader-installer_v0.5.0.elf (a download). They share a stem, and deleting
   the first would make /api/payloads/load answer "that one is not on this console yet" about a
   payload the build is holding, because a bundled stem is staged from PB_DIR/<bundled name>. */
static void dl_drop_same_stem(const char *final_path) {
    char dir[700], base[300], stem[128];
    snprintf(dir, sizeof(dir), "%s", final_path);
    char *sl = strrchr(dir, '/');
    if (!sl) return;
    *sl = 0;
    snprintf(base, sizeof(base), "%s", sl + 1);
    pms_file_stem(base, stem, sizeof(stem));
    if (!stem[0] || !dir[0]) return;
    DIR *d = opendir(dir);
    if (!d) return;
    struct dirent *de;
    while ((de = readdir(d)) != NULL) {
        if (de->d_name[0] == '.') continue;
        if (!strcmp(de->d_name, base)) continue;        /* the file we just put there */
        if (pms_name_is_bundled(de->d_name)) continue;  /* this build's own copy */
        char other[128];
        pms_file_stem(de->d_name, other, sizeof(other));
        if (!other[0] || strcmp(other, stem)) continue;
        char victim[900];
        snprintf(victim, sizeof(victim), "%s/%s", dir, de->d_name);
        if (unlink(victim) == 0) ilog("fetch: removed the previous %s (%s)", stem, de->d_name);
    }
    closedir(d);
}

/* ---- which release this console took, written down beside the file ----------------------
 * Without this the panel compares the CATALOGUE's version - the version the machine that BUILT
 * the catalogue was holding - so a payload updated on the console went on being offered the same
 * update for ever, and pressing Update fetched the same bytes again.
 *
 * ONE NOTE PER STEM, dot-prefixed: a newer release overwrites it rather than leaving a stale one
 * beside the new file, and every directory walk here skips dotfiles - which is also why the note
 * can never be mistaken for a payload.
 *
 * It is also the only honest evidence that THIS lane put a file in the folder, as opposed to a
 * copy some PC left there earlier. /api/payloads/load needs exactly that: see the note there. */
static void dl_note_path(const char *dir, const char *name, char *out, size_t outsz) {
    char stem[128];
    out[0] = 0;
    pms_file_stem(name, stem, sizeof(stem));
    if (!stem[0] || !dir || !dir[0]) return;
    snprintf(out, outsz, "%s/.%s.ver", dir, stem);
}

static void dl_take_note(const char *dir, const char *name, const char *tag) {
    char p[800];
    if (!tag || !tag[0]) return;
    dl_note_path(dir, name, p, sizeof(p));
    if (!p[0]) return;
    int f = open(p, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (f < 0) return;
    (void)write_all_checked(f, tag, strlen(tag));
    close(f);
}

/* The release recorded for this file's stem, or "" when this lane never fetched one. */
static void dl_taken_version(const char *dir, const char *name, char *out, size_t outsz) {
    char p[800];
    out[0] = 0;
    dl_note_path(dir, name, p, sizeof(p));
    if (!p[0]) return;
    int f = open(p, O_RDONLY);
    if (f < 0) return;
    ssize_t r = read(f, out, outsz - 1);
    close(f);
    if (r <= 0) { out[0] = 0; return; }
    out[r] = 0;
    /* A tag is one line; refuse anything that is not plainly one. */
    for (size_t i = 0; out[i]; i++)
        if (out[i] < 0x20 || out[i] == 0x7F) { out[i] = 0; break; }
}

/* Find the end of the JSON object beginning at `p` (which must point at '{'), respecting strings
   and escapes - brace counting that does not is how a release body containing a '}' ends an
   object early. Returns the position just past the closing brace, or NULL. */
static const char *json_obj_end(const char *p) {
    if (!p || *p != '{') return NULL;
    int depth = 0, instr = 0;
    for (const char *q = p; *q; q++) {
        if (instr) {
            if (*q == 0x5C && q[1]) { q++; continue; }
            if (*q == '"') instr = 0;
            continue;
        }
        if (*q == '"') { instr = 1; continue; }
        if (*q == '{') depth++;
        else if (*q == '}') { depth--; if (depth == 0) return q + 1; }
    }
    return NULL;
}

/* The newest release that is not a draft.

   NOT /releases/latest. GitHub defines that endpoint as "the newest release that is not a
   PRE-release", and the projects this panel follows ship pre-releases as a matter of course:
   measured on the PC side, where ShadowMountPlus had 1.7beta3 published while /releases/latest
   still answered 1.7beta2, so the panel reported everything up to date while a newer build sat
   on the project's own page. The companion reads the LIST endpoint for exactly that reason and
   this reads the same one - two lanes disagreeing about what the newest release is would be
   worse than either alone.

   A draft is skipped because it is not published and nobody can download it. */
static const char *release_choose(const char *json, const char **endp) {
    const char *p = json;
    *endp = NULL;
    if (!p) return NULL;
    while (*p && *p != '[' && *p != '{') p++;
    if (*p == '{') { *endp = json_obj_end(p); return *endp ? p : NULL; }
    if (*p != '[') return NULL;
    p++;
    for (;;) {
        while (*p == ' ' || *p == 0x0A || *p == 0x0D || *p == 0x09 || *p == ',') p++;
        if (*p != '{') return NULL;
        const char *e = json_obj_end(p);
        if (!e) return NULL;
        int is_draft = 0;
        const char *dr = strstr(p, "\"draft\"");
        if (dr && dr < e) {
            dr += 7;
            while (*dr == ' ' || *dr == ':') dr++;
            is_draft = !strncmp(dr, "true", 4);
        }
        if (!is_draft) { *endp = e; return p; }
        p = e;
    }
}

/* Ask a project what it has released. The buffer is the caller's because it is large: across the
   twenty projects this app follows, the list endpoint reaches 147 KB at per_page=3 and 287 KB at
   per_page=10 (PS5_RetroArch, measured), so a truncated read is a real possibility and is
   reported rather than parsed. per_page=3 is enough to step over a draft. */
#define GH_BUF_BYTES (320 * 1024)
static int github_releases(const char *repo, char *buf, size_t bufsz, char *err, size_t errsz) {
    char api[320];
    long long got = 0;
    snprintf(api, sizeof(api), "https://api.github.com/repos/%s/releases?per_page=3", repo);
    int st = https_get_text(api, buf, bufsz, &got, err, errsz);
    if (st == 200 && got >= (long long)bufsz - 1) {
        if (err && errsz)
            snprintf(err, errsz, "that project's release page is larger than this console reads "
                                 "(%lld bytes)", got);
        return -2;
    }
    return st;
}

/* One file attached to a release, as this console needs it. See the host table above for why
   `url` is the asset's API url and not browser_download_url. */
typedef struct {
    char      name[200];
    char      url[200];
    char      digest[72];           /* hex sha256, when the release states one */
    long long size;
} up_asset_t;

#define UP_ASSETS 12

/* Read one release's attached files. `end` bounds it to THAT release: without it an asset from an
   older release in the same list-endpoint reply could be read as this one's. Returns how many. */
static int release_assets(const char *json, const char *end, up_asset_t *out, int max) {
    int n = 0;
    const char *p = strstr(json, "\"assets\"");
    if (!p || (end && p >= end)) return 0;
    /* The name, size, digest and API url all sit BEFORE browser_download_url inside each asset
       object, so the window for them runs FORWARD from the previous asset's url (or the start of
       the array) up to this one. Walking BACK to the nearest '{' - what this did at first - lands
       inside the asset's own nested "uploader" object, and every field came back empty. */
    const char *win = p;
    while (n < max && (p = strstr(p, "\"browser_download_url\"")) != NULL) {
        if (end && p >= end) break;
        const char *cur = p;
        char dg[128] = {0};
        up_asset_t a;
        memset(&a, 0, sizeof(a));
        /* Bare key names: the json_* helpers add the quotes themselves - passing them already
           quoted made every lookup search for """name""" and find nothing. The FIRST "url" in
           the window is the asset's own; it precedes the uploader object, which has one too. */
        json_str_after_lim(win, cur, "name", a.name, sizeof(a.name));
        json_str_after_lim(win, cur, "url", a.url, sizeof(a.url));
        json_str_after_lim(win, cur, "digest", dg, sizeof(dg));
        if (!strncasecmp(dg, "sha256:", 7)) snprintf(a.digest, sizeof(a.digest), "%s", dg + 7);
        {
            const char *sp = strstr(win, "\"size\"");
            a.size = (sp && sp < cur) ? (long long)json_num_after(sp, "size", 0) : 0;
        }
        if (a.name[0] && a.url[0]) out[n++] = a;
        p = cur + 1;
        win = cur + 1;
    }
    return n;
}

/* ---------------- what projects have released, as this console last saw it ----------------
 * Read by the route, written only by the worker below. Six hours is the companion's TTL for the
 * same question and for the same reason: a release check is not urgent, and the allowance is 60
 * calls an hour from one address for anyone without a token.
 * ------------------------------------------------------------------------------------------ */
typedef struct {
    char       repo[160];
    char       tag[120], page[320], published[16], stable_tag[120];
    up_asset_t a[UP_ASSETS];
    int        na;
    int        prerelease;
    char       error[200];
    long long  at;                     /* when it was answered; 0 = never */
} upstream_t;

#define UP_MAX    40
#define UP_TTL_S  (6 * 3600)

static upstream_t      g_up[UP_MAX];
static char            g_up_q[UP_MAX][160];
static int             g_up_qn;
static int             g_up_busy;      /* a worker thread is running */
static pthread_mutex_t g_up_lock = PTHREAD_MUTEX_INITIALIZER;

/* Caller holds g_up_lock. */
static upstream_t *up_find(const char *repo) {
    for (int i = 0; i < UP_MAX; i++)
        if (g_up[i].repo[0] && !strcmp(g_up[i].repo, repo)) return &g_up[i];
    return NULL;
}

/* Caller holds g_up_lock. Takes the oldest slot when every one is used - a panel that follows
   more projects than this table holds should lose the least recently answered, not refuse. */
static upstream_t *up_slot(const char *repo) {
    upstream_t *e = up_find(repo);
    if (e) return e;
    upstream_t *best = &g_up[0];
    for (int i = 0; i < UP_MAX; i++) {
        if (!g_up[i].repo[0]) { best = &g_up[i]; break; }
        if (g_up[i].at < best->at) best = &g_up[i];
    }
    memset(best, 0, sizeof(*best));
    snprintf(best->repo, sizeof(best->repo), "%s", repo);
    return best;
}

static void *upstream_worker(void *arg) {
    (void)arg;
    char *json = (char *)malloc(GH_BUF_BYTES);
    for (;;) {
        char repo[160] = {0};
        pthread_mutex_lock(&g_up_lock);
        if (g_up_qn <= 0 || !json) {
            g_up_busy = 0;
            pthread_mutex_unlock(&g_up_lock);
            break;
        }
        snprintf(repo, sizeof(repo), "%s", g_up_q[0]);
        for (int i = 1; i < g_up_qn; i++) memcpy(g_up_q[i - 1], g_up_q[i], sizeof(g_up_q[0]));
        g_up_qn--;
        pthread_mutex_unlock(&g_up_lock);

        upstream_t r;
        memset(&r, 0, sizeof(r));
        snprintf(r.repo, sizeof(r.repo), "%s", repo);
        char err[200] = {0};
        int st = github_releases(repo, json, GH_BUF_BYTES, err, sizeof(err));
        if (st == 200) {
            const char *end = NULL;
            const char *rel = release_choose(json, &end);
            if (!rel) {
                /* A project can have tags and no releases at all. Say so plainly rather than
                   reporting an empty version, which reads as "up to date". */
                snprintf(r.error, sizeof(r.error), "no releases");
            } else {
                char pub[40] = {0};
                json_str_after_lim(rel, end, "tag_name", r.tag, sizeof(r.tag));
                json_str_after_lim(rel, end, "html_url", r.page, sizeof(r.page));
                json_str_after_lim(rel, end, "published_at", pub, sizeof(pub));
                snprintf(r.published, sizeof(r.published), "%.10s", pub);
                const char *pr = strstr(rel, "\"prerelease\"");
                if (pr && pr < end) {
                    pr += 12;
                    while (*pr == ' ' || *pr == ':') pr++;
                    r.prerelease = !strncmp(pr, "true", 4);
                }
                /* What the project itself calls stable, when that is a different release - the
                   panel says which, and /releases/latest hiding pre-releases is what started all
                   of this. */
                if (r.prerelease) {
                    const char *p2 = end;
                    for (int guard = 0; guard < UP_MAX && p2 && *p2; guard++) {
                        const char *e2 = NULL;
                        const char *r2 = release_choose(p2, &e2);
                        if (!r2 || !e2) break;
                        const char *q2 = strstr(r2, "\"prerelease\"");
                        int pre2 = 1;
                        if (q2 && q2 < e2) {
                            q2 += 12;
                            while (*q2 == ' ' || *q2 == ':') q2++;
                            pre2 = !strncmp(q2, "true", 4);
                        }
                        if (!pre2) {
                            json_str_after_lim(r2, e2, "tag_name",
                                               r.stable_tag, sizeof(r.stable_tag));
                            break;
                        }
                        p2 = e2;
                    }
                }
                r.na = release_assets(rel, end, r.a, UP_ASSETS);
            }
        } else if (st == 403 || st == 429) {
            snprintf(r.error, sizeof(r.error), "rate-limited");
        } else if (st == 404) {
            snprintf(r.error, sizeof(r.error), "not visible");
        } else if (st == -2) {
            snprintf(r.error, sizeof(r.error), "%s", err);
        } else {
            snprintf(r.error, sizeof(r.error), "%s", err[0] ? err : "could not reach GitHub");
        }
        /* Keep the document so the route can pick an asset per item without asking again. */
        pthread_mutex_lock(&g_up_lock);
        upstream_t *slot = up_slot(repo);
        char keep[160];
        snprintf(keep, sizeof(keep), "%s", slot->repo);
        *slot = r;
        snprintf(slot->repo, sizeof(slot->repo), "%s", keep);
        /* A FAILURE IS CACHED TOO, BRIEFLY. Without it a panel that polls while GitHub is
           refusing keeps asking and keeps being refused for the rest of the hour. */
        slot->at = (long long)time(NULL) - (r.error[0] ? (UP_TTL_S - 300) : 0);
        pthread_mutex_unlock(&g_up_lock);
    }
    if (json) free(json);
    return NULL;
}

/* Caller holds g_up_lock. Put a project in the queue unless it is already there. */
static void up_enqueue(const char *repo) {
    for (int i = 0; i < g_up_qn; i++) if (!strcmp(g_up_q[i], repo)) return;
    if (g_up_qn >= UP_MAX) return;
    snprintf(g_up_q[g_up_qn], sizeof(g_up_q[0]), "%s", repo);
    g_up_qn++;
    if (!g_up_busy) {
        pthread_t t;
        pthread_attr_t a;
        pthread_attr_init(&a);
        pthread_attr_setstacksize(&a, 512 * 1024);
        if (pthread_create(&t, &a, upstream_worker, NULL) == 0) {
            pthread_detach(t);
            g_up_busy = 1;
        }
        pthread_attr_destroy(&a);
    }
}

/* ---------------- moving a game between drives ---------------------------
 * A ShadowMount backup runs from wherever it sits, so "install" for a PS5 backup
 * really means "put it on the drive you want it to live on". Copy first, verify the
 * size, only then remove the original: a half-copied 90 GB game that has already
 * deleted its source is unrecoverable.
 *
 * Destinations are the paths ShadowMount actually scans (its config.ini lists the
 * drive root and <drive>/homebrew), so a moved game is picked up without any
 * further configuration.
 * ------------------------------------------------------------------------- */
typedef struct {
    int          active;
    int          done;
    int          ok;
    long long    total, copied;
    char         name[240];
    char         src[600], dst[600];
    char         error[200];
} move_job_t;

static move_job_t g_move;
static pthread_mutex_t g_move_lock = PTHREAD_MUTEX_INITIALIZER;

/* What may be the SOURCE of a move: the two tiers the delete lane trusts (a homebrew folder, or a
   file directly at a drive root), plus <drive>/pkg, which the console's own removable scan lists
   as a place a backup can sit before it is moved somewhere ShadowMount will see it. A regular file
   carrying a package or backup extension, nowhere else - a path outside these folders is not the
   app's to touch. A PKG is moved as a file, which is exactly what the removable scan promises
   when it marks every .pkg on a drive `movable` and the more-menu offers "Move to another drive"
   for it; installing that PKG stays a separate action. Refusing PKGs here turned that menu entry
   into a dead button for one file type. */
static int move_source_allowed(const char *src) {
    if (!src || src[0] != '/' || strstr(src, "..")) return 0;
    if (!path_ext_is(src, ".pkg") && !is_backup_ext(src)) return 0;
    if (under_homebrew_root(src)) return 1;
    for (int r = 0; DRIVE_ROOTS[r]; r++) {
        size_t l = strlen(DRIVE_ROOTS[r]);
        if (strncmp(src, DRIVE_ROOTS[r], l) || src[l] != '/') continue;
        const char *rest = src + l + 1;
        if (!rest[0]) return 0;
        if (!strchr(rest, '/')) return 1;                                   /* at the drive root */
        if (!strncmp(rest, "pkg/", 4) && rest[4] && !strchr(rest + 4, '/')) return 1;
    }
    return 0;
}

/* Map a drive id the UI can send to the folder a moved game should live in. */
static int move_dest_dir(const char *drive, char *out, size_t outsz) {
    if (!drive || !drive[0]) return -1;
    if (!strcmp(drive, "internal")) { snprintf(out, outsz, "/data/homebrew"); return 0; }
    if (!strncmp(drive, "ext", 3) || !strncmp(drive, "usb", 3)) {
        for (const char *c = drive + 3; *c; c++) if (*c < '0' || *c > '9') return -1;
        snprintf(out, outsz, "/mnt/%s/homebrew", drive);
        return 0;
    }
    return -1;
}

static void *move_thread(void *arg) {
    (void)arg;
    char src[600], dst[600], part[620];
    pthread_mutex_lock(&g_move_lock);
    snprintf(src, sizeof(src), "%s", g_move.src);
    snprintf(dst, sizeof(dst), "%s", g_move.dst);
    pthread_mutex_unlock(&g_move_lock);
    /* Copy to "<dst>.part" and rename at the end. The destination IS a ShadowMount watch folder,
       and ShadowMount mounts a container the instant its name appears - so writing the final name
       first meant a 90 GB game was mounted while it was still arriving, and the console had a
       broken title that crashed on launch until the copy finished. The PC lane, the fetch lane and
       the file API all stage the same way; this was the one writer that did not. */
    snprintf(part, sizeof(part), "%s.part", dst);

    int in = open(src, O_RDONLY);
    if (in < 0) {
        pthread_mutex_lock(&g_move_lock);
        snprintf(g_move.error, sizeof(g_move.error), "cannot read the source file");
        g_move.done = 1; g_move.active = 0;
        pthread_mutex_unlock(&g_move_lock);
        notifyf("Could not move %s\nIts file could not be opened - the drive it is on may have "
                "been disconnected. Nothing was removed.", g_move.name);
        return NULL;
    }
    unlink(part);                                 /* a leftover from an interrupted move */
    int out = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (out < 0) {
        close(in);
        pthread_mutex_lock(&g_move_lock);
        snprintf(g_move.error, sizeof(g_move.error), "cannot write to the destination drive");
        g_move.done = 1; g_move.active = 0;
        pthread_mutex_unlock(&g_move_lock);
        notifyf("Could not move %s\nThe destination drive would not accept it - check it is "
                "connected and has room. Nothing was removed.", g_move.name);
        return NULL;
    }
    char *buf = (char *)malloc(COPY_BUF_BYTES);   /* heap: worker stacks here are tiny */
    long long copied = 0;
    int failed = !buf;
    if (buf) {
        ssize_t r;
        while ((r = read(in, buf, COPY_BUF_BYTES)) > 0) {
            ssize_t off = 0;
            while (off < r) {
                ssize_t w = write(out, buf + off, (size_t)(r - off));
                if (w <= 0) { failed = 1; break; }
                off += w;
            }
            if (failed) break;
            copied += r;
            pthread_mutex_lock(&g_move_lock);
            g_move.copied = copied;
            pthread_mutex_unlock(&g_move_lock);
        }
        if (r < 0) failed = 1;
    }
    free(buf);
    close(in);
    close(out);

    struct stat ss, ds;
    int same = (stat(src, &ss) == 0 && stat(part, &ds) == 0 && ss.st_size == ds.st_size);
    if (failed || !same) {
        unlink(part);                             /* never leave a half file behind */
        pthread_mutex_lock(&g_move_lock);
        snprintf(g_move.error, sizeof(g_move.error),
                 failed ? "the copy failed - the destination drive may be full"
                        : "the copy did not match the original, nothing was removed");
        g_move.done = 1; g_move.active = 0; g_move.ok = 0;
        pthread_mutex_unlock(&g_move_lock);
        /* Second line is the REASON, not the title. It used to print g_move.name, so the toast
           read "Move failed / God of War" - the shape of a reason with none in it, while the real
           one sat in g_move.error and only ever reached /api/move/status. Its two sibling
           failure toasts both put a cause here. */
        notifyf("Could not move %s\n%s", g_move.name,
                failed ? "The copy failed - the destination drive may be full. Nothing was removed."
                       : "The copy did not match the original. Nothing was removed.");
        return NULL;
    }
    /* Every byte is across and verified: give it its real name. rename() onto an existing file
       is not portable here, so an older copy at the destination is removed first - the same
       replace-in-place the file API and the fetch lane perform. */
    unlink(dst);
    if (rename(part, dst) != 0) {
        unlink(part);
        pthread_mutex_lock(&g_move_lock);
        snprintf(g_move.error, sizeof(g_move.error),
                 "the copy finished but could not be put in place on that drive, nothing was removed");
        g_move.done = 1; g_move.active = 0; g_move.ok = 0;
        pthread_mutex_unlock(&g_move_lock);
        notifyf("Could not move %s\nThe copy finished but could not be put in place on that "
                "drive. Nothing was removed.", g_move.name);
        return NULL;
    }
    unlink(src);                                  /* only now is it safe */
    pthread_mutex_lock(&g_move_lock);
    g_move.ok = 1; g_move.done = 1; g_move.active = 0;
    pthread_mutex_unlock(&g_move_lock);
    notifyf("%s has moved\nIt is ready to play from its new drive", g_move.name);
    return NULL;
}

/* Case-insensitive substring, for HTTP headers (strcasestr is not portable here). */
static const char *strcasestr_local(const char *hay, const char *needle) {
    size_t nl = strlen(needle);
    if (!nl) return hay;
    for (const char *p = hay; *p; p++) {
        size_t i = 0;
        while (i < nl) {
            char a = p[i], b = needle[i];
            if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
            if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
            if (a != b) break;
            i++;
        }
        if (i == nl) return p;
    }
    return NULL;
}

/* ---------------- POST: the mods panel writes through here ----------------
 * The UI toggles a cheat with POST /api/mods/<TID>/toggle and a small JSON body.
 * The server used to answer GET only, so every toggle got a 405 and the console
 * app could list cheats but never apply one.
 * ------------------------------------------------------------------------- */

/* Number after "key": in a small flat JSON body. Returns def when absent. */
static long json_num_after(const char *json, const char *key, long def) {
    if (!json) return def;
    char pat[64];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *p = strstr(json, pat);
    if (!p) return def;
    p = strchr(p + strlen(pat), ':');
    if (!p) return def;
    p++;
    while (*p == ' ' || *p == '\t') p++;
    if (!strncmp(p, "true", 4)) return 1;
    if (!strncmp(p, "false", 5)) return 0;
    if (!strncmp(p, "null", 4)) return def;
    char *end = NULL;
    long v = strtol(p, &end, 10);
    return (end && end != p) ? v : def;
}

/* Resolve the cheat file for a title using its installed version (same rule the
   GET side uses), so a toggle acts on exactly the file the panel is showing. */
/* WHICH FILE DOES THIS REQUEST MEAN? Without a version, the installed one. With a version, THAT
   version's file - and if the library has no file for exactly that version the request is REFUSED
   rather than quietly served from a neighbour, because a mod is applied by its INDEX into whatever
   the panel listed. Returns 0 resolved, -1 nothing at all, -2 nothing for that exact version.
   The PS4 carries the same pair for the same reason (ps4-app/onconsole/server_ps4.c). */
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

static int mods_file_for(const char *tid, char *out, size_t outsz) {
    char iver[24] = {0};
    title_t *rows = (title_t *)calloc(MAX_TITLES, sizeof(title_t));
    int n = rows ? read_console_titles(rows, MAX_TITLES) : -1;
    for (int i = 0; i < n; i++)
        if (!strcmp(rows[i].tid, tid)) { snprintf(iver, sizeof(iver), "%s", rows[i].ver); break; }
    free(rows);
    const char *why = "none";
    cheat_pick_file(tid, iver, out, outsz, &why);
    return out[0] ? 0 : -1;
}

static void handle_post(int fd, const char *rawpath, const char *body) {
    char path[1024];
    snprintf(path, sizeof(path), "%s", rawpath);
    char *qs = strchr(path, '?'); if (qs) *qs = 0;

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

        /* "select" exists only because CheatRunner needed an active document; we
           resolve the file per request, so acknowledging it keeps the UI happy. */
        if (!strcmp(action, "select")) { send_json(fd, "{\"ok\":true,\"selected\":true}"); return; }

        /* A game PATCH is a different thing from a cheat: it comes from the XML patch library,
           is applied once instead of toggled, and a title can have patches without having any
           cheat file at all — so this must be handled BEFORE the cheat-file lookup below.
           "apply" used to be a silent alias for "toggle", which would have flipped an unrelated
           CHEAT the moment the patch list stopped being empty. */
        if (!strcmp(action, "apply") || !strcmp(action, "unapply")) {
            char o[1400];
            patch_action_json(tid, (int)json_num_after(body, "index", 0),
                              (int)json_num_after(body, "force", 0),
                              (int)json_num_after(body, "dry", 0),
                              !strcmp(action, "unapply"), o, sizeof(o));
            send_json(fd, o);
            return;
        }

        /* THE SAME FILE THE PANEL LISTED - see mods_file_for_req. A mod is applied by INDEX, so a
           mismatch here applies a different cheat and reports ok:true under the wrong name. */
        char wver[48] = {0};
        json_str_after(body ? body : "", "version", wver, sizeof(wver));
        char file[600];
        int frc = mods_file_for_req(tid, wver, file, sizeof(file));
        if (frc == -2) {
            char ev[110], o[520];
            json_escape(wver, ev, sizeof(ev));
            snprintf(o, sizeof(o),
                     "{\"ok\":false,\"error\":\"no_cheat_file_for_version\",\"version\":\"%s\","
                     "\"message\":\"There is no cheat file for that version of this game, so nothing "
                     "was changed. Pick a version the list offers.\"}", ev);
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
            send_json(fd, "{\"ok\":false,\"error\":\"game_not_running\","
                          "\"message\":\"Launch the game first - cheats are written into its live memory.\"}");
            return;
        }

        if (!strcmp(action, "disable-all")) {
            int non_json = 0;
            char *doc = cheat_load_doc(file, &non_json);
            if (!doc) { send_json(fd, "{\"ok\":false,\"error\":\"cannot read cheat file\"}"); return; }
            /* Three outcomes, not two. rc > 0 means bytes were actually put back; rc == 0 means
               that mod was already off and nothing needed doing. Counting the second as "reverted"
               inflated the number, and `failed` was computed and then never mentioned - so a run
               where 3 were undone and 2 refused still announced a clean sweep. */
            int reverted = 0, already = 0, failed = 0;
            /* The document is loaded (and for .mc4, decrypted) ONCE, here, and each block is
               handed straight to the engine. cheat_apply_mod(file, ...) re-read and re-decrypted
               the file for every mod, on the accept loop, so a 96-mod file stalled every other
               client for the whole sweep. Every "off" write is still gated on the memory holding
               a documented state - the same gate as a single toggle. */
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
    if (!strcmp(path, "/api/cheats/rescan") || !strcmp(path, "/api/cheat/rescan")) {
        mkdir(CHEAT_INBOX_DIR, 0777);
        int filed = cheat_intake_all();
        char o[240];
        snprintf(o, sizeof(o), "{\"ok\":true,\"filed\":%d}", filed);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/move")) {
        /* {"path":"/mnt/usb0/...ffpfsc","drive":"ext1","name":"..."} */
        char src[600] = {0}, drive[24] = {0}, nm[240] = {0};
        json_str_after(body ? body : "", "path", src, sizeof(src));
        json_str_after(body ? body : "", "drive", drive, sizeof(drive));
        json_str_after(body ? body : "", "name", nm, sizeof(nm));
        if (!src[0] || !drive[0]) { send_json(fd, "{\"ok\":false,\"error\":\"need path and drive\"}"); return; }
        /* The SAME boundary the delete lane keeps. This accepted any absolute regular file, so a
           LAN request could copy the console's own database into a USB homebrew folder and then
           unlink the original - the delete lane had bounded itself carefully and the move undid
           that. A package or backup container, inside a folder that exists to hold one, and
           nothing else. */
        if (!move_source_allowed(src)) {
            send_json(fd, "{\"ok\":false,\"error\":\"Only a package or game backup in a drive's "
                          "root, homebrew or pkg folder can be moved between drives\"}");
            return;
        }
        struct stat st;
        if (lstat(src, &st) != 0 || !S_ISREG(st.st_mode)) {
            send_json(fd, "{\"ok\":false,\"error\":\"source file not found\"}"); return;
        }
        char destdir[200];
        if (move_dest_dir(drive, destdir, sizeof(destdir)) != 0) {
            send_json(fd, "{\"ok\":false,\"error\":\"unknown destination drive\"}"); return;
        }
        if (strcmp(drive, "internal") != 0) {
            /* Is that drive REALLY there? An unmounted /mnt/usbN opens fine - it resolves to the
               parent filesystem - so mkdir below happily created /mnt/usbN/homebrew on the SYSTEM
               partition, statvfs reported the system partition's free space, and a game under
               31 GB was copied there, its source removed, and then hidden the moment a real drive
               was plugged in. Same st_dev test /api/devices uses to decide `detected`. */
            char droot[64];
            snprintf(droot, sizeof(droot), "/mnt/%s", drive);
            struct stat dstat, mstat;
            if (stat(droot, &dstat) != 0 ||
                (stat("/mnt", &mstat) == 0 && dstat.st_dev == mstat.st_dev)) {
                send_json(fd, "{\"ok\":false,\"error\":\"That drive is not connected\"}");
                return;
            }
        }
        mkdir(destdir, 0777);
        const char *base = strrchr(src, '/');
        base = base ? base + 1 : src;
        char dst[600];
        snprintf(dst, sizeof(dst), "%s/%s", destdir, base);
        if (!strcmp(dst, src)) { send_json(fd, "{\"ok\":false,\"error\":\"already on that drive\"}"); return; }
        struct statvfs vfs;
        if (statvfs(destdir, &vfs) == 0) {
            long long freeb = (long long)vfs.f_bavail * (long long)vfs.f_frsize;
            if (freeb > 0 && freeb < (long long)st.st_size) {
                char o[260];
                snprintf(o, sizeof(o),
                         "{\"ok\":false,\"error\":\"not enough space on that drive "
                         "(needs %lld GB, %lld GB free)\"}",
                         (long long)st.st_size / 1000000000LL, freeb / 1000000000LL);
                send_json(fd, o);
                return;
            }
        }
        pthread_mutex_lock(&g_move_lock);
        if (g_move.active) {
            pthread_mutex_unlock(&g_move_lock);
            send_json(fd, "{\"ok\":false,\"error\":\"a move is already running\"}");
            return;
        }
        memset(&g_move, 0, sizeof(g_move));
        g_move.active = 1;
        g_move.total = (long long)st.st_size;
        snprintf(g_move.src, sizeof(g_move.src), "%s", src);
        snprintf(g_move.dst, sizeof(g_move.dst), "%s", dst);
        snprintf(g_move.name, sizeof(g_move.name), "%s", nm[0] ? nm : base);
        pthread_mutex_unlock(&g_move_lock);

        pthread_t t;
        pthread_attr_t attr;
        pthread_attr_init(&attr);
        pthread_attr_setstacksize(&attr, 256 * 1024);
        int rc = pthread_create(&t, &attr, move_thread, NULL);
        pthread_attr_destroy(&attr);
        if (rc != 0) {
            pthread_mutex_lock(&g_move_lock);
            g_move.active = 0;
            pthread_mutex_unlock(&g_move_lock);
            send_json(fd, "{\"ok\":false,\"error\":\"could not start the move\"}");
            return;
        }
        notifyf("Moving %s to %s\nIt stays playable the whole time - nothing is removed "
                "until the copy is verified", g_move.name, drive);
        char eo[700], o[900];
        json_escape(dst, eo, sizeof(eo));
        snprintf(o, sizeof(o), "{\"ok\":true,\"started\":true,\"dest\":\"%s\",\"bytes\":%lld}",
                 eo, (long long)st.st_size);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/install")) {
        /* The package is already on this console (USB stick, external drive, internal),
           so nothing is downloaded - but it still goes through the SAME installer as a
           downloaded game: we serve it over HTTP and hand the URL to the install daemon.
           sceAppInstUtilAppInstallPkg is NOT a substitute; it registers metadata only and
           leaves a tile that fails with "Cannot start the game". */
        char key[1200] = {0}, file[1200] = {0}, nm[240] = {0};
        json_str_after(body ? body : "", "install_key", key, sizeof(key));
        json_str_after(body ? body : "", "name", nm, sizeof(nm));
        if (!strncmp(key, "local:", 6)) snprintf(file, sizeof(file), "%s", key + 6);
        else json_str_after(body ? body : "", "path", file, sizeof(file));
        if (!file[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"Not on the console. With the PC companion off, "
                          "only packages already on the PS5 (USB or internal) can be installed.\"}");
            return;
        }
        struct stat stt;
        if (stat(file, &stt) != 0 || !S_ISREG(stt.st_mode)) {
            send_json(fd, "{\"ok\":false,\"error\":\"file not found on console\"}");
            return;
        }
        if (!pkgfile_path_allowed(file)) {
            send_json(fd, "{\"ok\":false,\"error\":\"that file is not on a drive we can serve from\"}");
            return;
        }
        /* No :12800 gate any more. This lane used to require a third-party DPI daemon to be
           listening; it now goes through our own engine, which is spawned per install and needs
           nothing to be running beforehand except Payload Manager. */
        /* "+ Queue" sends mode:"queued" - hold it instead of starting, so the button
           behaves the same whether or not the PC companion is running. */
        char mode[24] = {0};
        json_str_after(body ? body : "", "mode", mode, sizeof(mode));
        int hold = !strcmp(mode, "queued");

        pthread_mutex_lock(&g_linst_lock);
        if (g_linst.active) {
            pthread_mutex_unlock(&g_linst_lock);
            send_json(fd, "{\"ok\":false,\"error\":\"an install is already running\"}");
            return;
        }
        /* Hand the daemon a clean TOKEN url. It cannot fetch a percent-escaped URL, and
           real package names are full of spaces and brackets - proven on device: the same
           request with a clean name returns {"res":"0"}, the escaped one "install failed".
           Registered only now, AFTER the already-running refusal: the ring has 8 slots, and
           registering before the check let eight refused POSTs during a multi-GB install evict
           the slot the live transfer was being served from, so the daemon's next Range request
           got a 403 mid-install. A refused request now leaves the ring alone. */
        int tok = pkgserve_register(file);
        char url[128];
        snprintf(url, sizeof(url), "http://127.0.0.1:%d/pkgfile/%d.pkg", PORT, tok);
        memset(&g_linst, 0, sizeof(g_linst));
        g_linst.active = hold ? 0 : 1;
        g_linst.held = hold;
        snprintf(g_linst.name, sizeof(g_linst.name), "%s", nm[0] ? nm : "package");
        snprintf(g_linst.path, sizeof(g_linst.path), "%s", file);
        snprintf(g_linst.url, sizeof(g_linst.url), "%s", url);
        snprintf(g_linst.msg, sizeof(g_linst.msg), hold ? "Held - press Start" : "Installing");
        pthread_mutex_unlock(&g_linst_lock);

        if (hold) {
            char eh[300], oh[600];
            json_escape(nm[0] ? nm : "package", eh, sizeof(eh));
            snprintf(oh, sizeof(oh),
                     "{\"ok\":true,\"lane\":\"console-local\",\"queued\":true,\"held\":true,"
                     "\"name\":\"%s\",\"message\":\"Added to the queue. Press Start to install it.\"}", eh);
            send_json(fd, oh);
            return;
        }

        pthread_t it;
        pthread_attr_t ia;
        pthread_attr_init(&ia);
        pthread_attr_setstacksize(&ia, 256 * 1024);
        int irc = pthread_create(&it, &ia, localinst_thread, NULL);
        pthread_attr_destroy(&ia);
        if (irc != 0) {
            pthread_mutex_lock(&g_linst_lock);
            g_linst.active = 0;
            pthread_mutex_unlock(&g_linst_lock);
            send_json(fd, "{\"ok\":false,\"error\":\"could not start the install\"}");
            return;
        }
        pthread_detach(it);
        notifyf("Installing %s\nReading it straight from the drive - the console takes it from here",
                nm[0] ? nm : "this package");
        char eu[200], o[700];
        json_escape(url, eu, sizeof(eu));
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"lane\":\"console-local\",\"started\":true,\"dest\":\"console\","
                 "\"via\":\"install-daemon\",\"url\":\"%s\","
                 "\"message\":\"Installing on the console. Watch the PS5 for the finished "
                 "notification, or the install status in the app.\"}", eu);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/game/delete")) {
        /* THE SAME ROUTE THE COMPANION EXPOSES, so the UI can call one thing wherever it is served
           from. With no PC on the network this page IS served by the console, every api() call
           lands here, and without this the Delete button would have quietly done nothing - the
           unknown-/api/ stub answers 200 {}.

           The body carries a title id; everything else - finding the container, the running-game
           check, the homebrew boundary - is the GET handler's, reached by forwarding. */
        char tid[16] = {0};
        const char *k = body ? strstr(body, "\"title_id\"") : NULL;
        if (k) {
            const char *c = strchr(k, ':');
            const char *q1 = c ? strchr(c, '"') : NULL;
            const char *q2 = q1 ? strchr(q1 + 1, '"') : NULL;
            if (q1 && q2 && (size_t)(q2 - q1 - 1) < sizeof(tid))
                snprintf(tid, sizeof(tid), "%.*s", (int)(q2 - q1 - 1), q1 + 1);
        }
        if (!tid[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"which game? no title id was given\"}");
            return;
        }
        char fwd[64];
        snprintf(fwd, sizeof(fwd), "/api/game/delete-backup?tid=%s", tid);
        handle(fd, fwd, NULL);      /* one implementation, two verbs; an API route, never static */
        return;
    }
    /* The UI drives the queue with POST; delegate to the same handlers the GET side
       uses so start / clear / cancel behave identically either way. */
    if (!strcmp(path, "/api/queue/start") || !strcmp(path, "/api/queue/clear") ||
        !strcmp(path, "/api/queue")) {
        handle(fd, path, NULL);
        return;
    }
    if (!strncmp(path, "/api/queue/", 11) && strstr(path, "/dismiss")) {
        /* "Get this row out of my way." The UI sends this for a row that has already finished, so
           it only has to clear the slot - there is nothing to stop. Refusing while an install is
           running is the one case worth a sentence, because clearing the slot underneath a running
           install would lose the verdict that says whether it worked.

           This used to fall through to "not supported on console", which the caller ignores - so
           the X did nothing at all and the row sat there. */
        pthread_mutex_lock(&g_linst_lock);
        int running = g_linst.active;
        if (!running) memset(&g_linst, 0, sizeof(g_linst));
        pthread_mutex_unlock(&g_linst_lock);
        send_json(fd, running ? "{\"ok\":false,\"error\":\"That install is still running. "
                                "Let it finish, then this row will clear.\"}"
                              : "{\"ok\":true,\"dismissed\":true}");
        return;
    }
    if (!strncmp(path, "/api/queue/", 11) && strstr(path, "/retry")) {
        /* Run the same package again. The slot still holds the url and the on-drive path from the
           attempt that failed, so nothing has to be re-sent from the app - which is the point of
           retry: it is the control you reach for when there is no PC to re-send anything. */
        pthread_mutex_lock(&g_linst_lock);
        int can = !g_linst.active && (g_linst.url[0] || g_linst.path[0]);
        if (can) {
            g_linst.held = 0; g_linst.active = 1; g_linst.done = 0;
            g_linst.ok = 0; g_linst.accepted_only = 0;
            snprintf(g_linst.msg, sizeof(g_linst.msg), "Installing");
        }
        int running = g_linst.active && !can;
        char nm3[240];
        snprintf(nm3, sizeof(nm3), "%s", g_linst.name);
        pthread_mutex_unlock(&g_linst_lock);
        if (!can) {
            send_json(fd, running ? "{\"ok\":false,\"error\":\"That install is already running.\"}"
                                  : "{\"ok\":false,\"error\":\"There is nothing to retry - send "
                                    "the package again from the library.\"}");
            return;
        }
        pthread_t rt;
        pthread_attr_t ra;
        pthread_attr_init(&ra);
        pthread_attr_setstacksize(&ra, 256 * 1024);
        int rrc = pthread_create(&rt, &ra, localinst_thread, NULL);
        pthread_attr_destroy(&ra);
        if (rrc != 0) {
            pthread_mutex_lock(&g_linst_lock);
            g_linst.active = 0; g_linst.done = 1; g_linst.ok = 0;
            pthread_mutex_unlock(&g_linst_lock);
            send_json(fd, "{\"ok\":false,\"error\":\"could not start the install\"}");
            return;
        }
        pthread_detach(rt);
        notifyf("Trying %s again\nSame package, fresh attempt - watch here for the result", nm3);
        send_json(fd, "{\"ok\":true,\"retried\":true}");
        return;
    }
    if (!strncmp(path, "/api/queue/", 11) && strstr(path, "/cancel")) {
        /* Cancelling a held job just clears the slot; a running one is left to finish,
           because the console installer has no safe abort. */
        pthread_mutex_lock(&g_linst_lock);
        int was_held = g_linst.held && !g_linst.active;
        if (was_held) memset(&g_linst, 0, sizeof(g_linst));
        pthread_mutex_unlock(&g_linst_lock);
        send_json(fd, was_held ? "{\"ok\":true}"
                               : "{\"ok\":false,\"error\":\"that install is already running\"}");
        return;
    }
    if (!strcmp(path, "/api/config")) { send_json(fd, "{\"ok\":true,\"saved\":false,\"on_console\":true}"); return; }
    send_json(fd, "{\"ok\":false,\"error\":\"not supported on console\"}");
}

/* ---------------- power-state watcher (OBSERVE ONLY) ----------------------
 * The comment just below this block used to assert "a PS5 payload gets no suspend
 * notification". That is FALSE, and believing it shaped every rest-mode decision here.
 *
 * The firmware publishes power state into four NAMED kernel event flags that any process may
 * open by name. Proven on this console: shadowmountplus.elf - a payload we ourselves ship -
 * imports sceKernelOpenEventFlag/PollEventFlag/CloseEventFlag, and its own log shows
 * "[SHELLFLAG] monitor started (4/4 flags opened)" plus
 * "SceSystemStateMgrInfo initial=0x00000003000003E8 decoded=STATE=WORKING(0x03E8)".
 * The API shape below is taken from that project (ShadowMountPlus, GPL-3.0,
 * src/sm_shellcore_flags.c). This project is GPLv3, so that is licence-compatible reuse.
 *
 * THIS BUILD ONLY WATCHES. It stops nothing and writes nothing. Its whole purpose is to measure
 * the one number nobody has: the RUNWAY between the first pre-suspend edge and the console
 * actually powering down. Until that is measured, acting on the edge is a coin flip with a
 * kernel panic on the losing side - and the manual button already works.
 *
 * TWO HARD RULES, both load-bearing:
 *   1. wait_mode is OR (2) and the mask is ALL BITS, so we CONSUME NOTHING. A clear mode
 *      (CLEAR_ALL 0x10 / CLEAR_PAT 0x20) would eat power-transition bits out from under
 *      ShellUI mid-shutdown - the one genuine way this could wedge standby or power-off.
 *   2. A negative return means the out-pattern was NOT written. Treating that as "value is 0"
 *      corrupts delta tracking and invents transitions that never happened.
 * ------------------------------------------------------------------------- */
typedef intptr_t evflag_t;
extern int sceKernelOpenEventFlag(evflag_t *ef, const char *name);
extern int sceKernelPollEventFlag(evflag_t ef, unsigned long long bits,
                                  unsigned int wait_mode, unsigned long long *out);
extern int sceKernelCloseEventFlag(evflag_t ef);

#define PWR_WAITMODE_OR   2u
#define PWR_ALL_BITS      0xFFFFFFFFFFFFFFFFULL
#define PWR_POLL_US       200000            /* 200 ms, same order as the reference */
#define PWR_ST_SHUTDOWN   100u
#define PWR_ST_SUSPEND    300u
#define PWR_ST_STANDBY    500u
#define PWR_ST_WORKING    1000u
#define PWR_SHELLUI_SHUTDOWN 0x0000000000200000ULL

static const char *PWR_FLAG_NAMES[4] = {
    "SceSystemStateMgrInfo", "SceSystemStateMgrStatus",
    "SceLncUtilSystemStatus", "SceShellCoreUtilAppFocus"
};

#define PWR_LOG_MAX 192
typedef struct { long long ms; long long real_s; char text[176]; } pwr_ev_t;
static pwr_ev_t g_pwr_log[PWR_LOG_MAX];
static int g_pwr_log_n = 0;                 /* total ever written; may exceed PWR_LOG_MAX */
static pthread_mutex_t g_pwr_lock = PTHREAD_MUTEX_INITIALIZER;
static evflag_t g_pwr_h[4];
static int g_pwr_opened = 0;
static unsigned long long g_pwr_val[4];
static int g_pwr_have[4];
static int g_pwr_rc[4];

static const char *pwr_state_name(unsigned st) {
    switch (st) {
        case PWR_ST_SHUTDOWN: return "SHUTDOWN_ON_GOING";
        case PWR_ST_SUSPEND:  return "SUSPEND_ON_GOING";
        case PWR_ST_STANDBY:  return "MAIN_ON_STANDBY";
        case PWR_ST_WORKING:  return "WORKING";
        case 0:               return "INVALID";
        case 10:              return "INITIALIZING";
        case 200:             return "POWER_SAVING";
        default:              return "UNKNOWN";
    }
}

/* The whole point of this watcher is to capture what happens as the console SHUTS DOWN, and a
   RAM ring buffer is erased by exactly the event we are trying to measure. So every entry is also
   appended to disk, immediately and synchronously.

   Why this is safe in a power transition, stated plainly:
     - It is a plain O_APPEND write to OUR OWN log file. It is not a database, not a Sony file,
       and nothing else reads it. A torn final line costs us one line, never consistency.
     - O_SYNC means the bytes are down before the call returns. Buffering would lose precisely
       the last few lines - the ones describing the shutdown.
     - It only fires on CHANGE, not on every poll. Steady state writes nothing at all, so this
       adds no I/O to normal running.
     - It appends ACROSS boots and is trimmed only at startup, so after a power-cycle the previous
       boot's shutdown lines are still there to read. That is the measurement. */
#define PWR_LOG_PATH "/data/pkg-mutant-shop/power.log"
#define PWR_LOG_MAX_BYTES (256 * 1024)

static void pwr_log_disk(long long real_s, long long ms, const char *text) {
    int fd = open(PWR_LOG_PATH, O_WRONLY | O_CREAT | O_APPEND | O_SYNC, 0666);
    if (fd < 0) return;
    char line[288];
    int n = snprintf(line, sizeof(line), "%lld\t%lld\t%s\n", real_s, ms, text);
    if (n > 0) {
        ssize_t w = write(fd, line, (size_t)n);
        (void)w;
    }
    close(fd);
}

/* Keep the file bounded. Runs once at startup, never during a transition: truncating a log while
   the console is going down is exactly the kind of write we do not want in that window. */
static void pwr_log_trim_at_boot(void) {
    struct stat st;
    if (stat(PWR_LOG_PATH, &st) != 0 || st.st_size <= PWR_LOG_MAX_BYTES) return;
    int fd = open(PWR_LOG_PATH, O_RDONLY);
    if (fd < 0) return;
    size_t keep = PWR_LOG_MAX_BYTES / 2;
    if (lseek(fd, (off_t)(st.st_size - (off_t)keep), SEEK_SET) < 0) { close(fd); return; }
    char *buf = (char *)malloc(keep + 1);
    if (!buf) { close(fd); return; }
    ssize_t got = read(fd, buf, keep);
    close(fd);
    if (got > 0) {
        int out = open(PWR_LOG_PATH ".tmp", O_WRONLY | O_CREAT | O_TRUNC, 0666);
        if (out >= 0) {
            ssize_t w = write(out, buf, (size_t)got);
            (void)w;
            close(out);
            rename(PWR_LOG_PATH ".tmp", PWR_LOG_PATH);
        }
    }
    free(buf);
}

static void pwr_logf(const char *fmt, ...) {
    struct timespec rt;
    clock_gettime(CLOCK_REALTIME, &rt);
    pthread_mutex_lock(&g_pwr_lock);
    pwr_ev_t *e = &g_pwr_log[g_pwr_log_n % PWR_LOG_MAX];
    e->ms = now_ms_local();
    e->real_s = (long long)rt.tv_sec;
    va_list ap;
    va_start(ap, fmt);
    vsnprintf(e->text, sizeof(e->text), fmt, ap);
    va_end(ap);
    g_pwr_log_n++;
    long long rs = e->real_s, rms = e->ms;
    char copy[176];
    snprintf(copy, sizeof(copy), "%s", e->text);
    pthread_mutex_unlock(&g_pwr_lock);
    pwr_log_disk(rs, rms, copy);          /* outside the lock: never hold it across a synced write */
}

static void *power_watchdog(void *arg) {
    (void)arg;
    int opened = 0;
    for (int i = 0; i < 4; i++) {
        g_pwr_h[i] = 0;
        int rc = sceKernelOpenEventFlag(&g_pwr_h[i], PWR_FLAG_NAMES[i]);
        if (rc == 0 && g_pwr_h[i]) {
            opened++;
        } else {
            g_pwr_h[i] = 0;
            pwr_logf("open %s FAILED rc=0x%x", PWR_FLAG_NAMES[i], (unsigned)rc);
        }
    }
    g_pwr_opened = opened;
    pwr_log_trim_at_boot();               /* bound the file once, while nothing is happening */
    pwr_logf("==== BOOT: PKG MUTANT SHOP %s, monitor started (%d/4 flags opened)",
             SHOP_VERSION, opened);
    if (!opened) return NULL;               /* nothing to watch - do not spin a pointless thread */

    unsigned prev_state = 0;
    int first = 1, said_shutdown = 0;
    for (;;) {
        usleep(PWR_POLL_US);
        for (int i = 0; i < 4; i++) {
            if (!g_pwr_h[i]) continue;
            unsigned long long v = 0;
            int rc = sceKernelPollEventFlag(g_pwr_h[i], PWR_ALL_BITS, PWR_WAITMODE_OR, &v);
            if (rc < 0) {
                /* out-pattern NOT written on error: keep the last good value. Log each distinct
                   error code once per flag so a steady EBUSY cannot flood the ring buffer. */
                if (g_pwr_rc[i] != rc) {
                    g_pwr_rc[i] = rc;
                    pwr_logf("%s poll rc=0x%x (value kept)", PWR_FLAG_NAMES[i], (unsigned)rc);
                }
                continue;
            }
            g_pwr_rc[i] = 0;
            if (!g_pwr_have[i] || g_pwr_val[i] != v) {
                unsigned long long old = g_pwr_val[i];
                int had = g_pwr_have[i];
                g_pwr_val[i] = v;
                g_pwr_have[i] = 1;
                if (had)
                    pwr_logf("%s 0x%016llx -> 0x%016llx (delta 0x%016llx)",
                             PWR_FLAG_NAMES[i], old, v, old ^ v);
                else
                    pwr_logf("%s initial 0x%016llx", PWR_FLAG_NAMES[i], v);
            }
        }
        /* The transition that matters: SceSystemStateMgrInfo low 16 bits. */
        if (g_pwr_have[0]) {
            unsigned st = (unsigned)(g_pwr_val[0] & 0xFFFFULL);
            if (first) {
                prev_state = st;
                first = 0;
                pwr_logf("STATE at start %s(%u)", pwr_state_name(st), st);
            } else if (st != prev_state) {
                unsigned trig = (unsigned)((g_pwr_val[0] >> 32) & 0xFFFFULL);
                pwr_logf("STATE %s(%u) -> %s(%u) trigger=0x%04x",
                         pwr_state_name(prev_state), prev_state, pwr_state_name(st), st, trig);
                prev_state = st;
            }
        }
        if (g_pwr_have[1]) {
            int on = (g_pwr_val[1] & PWR_SHELLUI_SHUTDOWN) ? 1 : 0;
            if (on != said_shutdown) {
                said_shutdown = on;
                pwr_logf("SHELLUI_SHUTDOWN_IN_PROGRESS %s", on ? "SET" : "cleared");
            }
        }
    }
    return NULL;
}

/* ---------------- rest mode -----------------------------------------------
 * A PS5 payload gets no suspend notification, but a suspend leaves a fingerprint:
 * CLOCK_REALTIME advances while CLOCK_MONOTONIC does not. A watchdog samples both
 * once a second; a gap far larger than the sampling interval means the console was
 * asleep. On waking we throw away everything that was tied to the old network
 * state (peer libraries, the PC list is kept but re-verified on next use) instead
 * of operating on sockets that died while we were suspended.
 * ------------------------------------------------------------------------- */
static volatile int g_resumed_count = 0;
static volatile long long g_last_resume_ms = 0;

static void *rest_watchdog(void *arg) {
    (void)arg;
    struct timespec rt, mt;
    clock_gettime(CLOCK_REALTIME, &rt);
    clock_gettime(CLOCK_MONOTONIC, &mt);
    long long prev_real = (long long)rt.tv_sec;
    long long prev_mono = (long long)mt.tv_sec;
    for (;;) {
        sleep(1);
        clock_gettime(CLOCK_REALTIME, &rt);
        clock_gettime(CLOCK_MONOTONIC, &mt);
        long long dr = (long long)rt.tv_sec - prev_real;
        long long dm = (long long)mt.tv_sec - prev_mono;
        prev_real = (long long)rt.tv_sec;
        prev_mono = (long long)mt.tv_sec;
        /* Both clocks jumping together is just a slow tick; realtime running far
           ahead of monotonic is a suspend we slept through. */
        if (dr - dm >= 20 || dm >= 20) {
            g_resumed_count++;
            g_last_resume_ms = now_ms_local();
            peer_cache_invalidate();     /* those libraries were fetched over a dead network */
            notifyf("PKG MUTANT SHOP is awake\nLooking for your PCs again - the library "
                    "may take a moment to fill in");
        }
    }
    return NULL;
}

/* ---------------- honest local service probing ---------------------------
 * The settings LEDs used to be filled in by the PC companion, so with the PC off
 * they all read as dashes. The console can answer this itself: connect to the
 * port on loopback and see whether anything is listening.
 * ------------------------------------------------------------------------- */
static int port_open(int port) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return 0;
    struct timeval tv = { 0, 250000 };            /* 250 ms is plenty on loopback */
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, &tv, sizeof(tv));
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, &tv, sizeof(tv));
    struct sockaddr_in a;
    memset(&a, 0, sizeof(a));
    a.sin_family = AF_INET;
    a.sin_port = htons((uint16_t)port);
    a.sin_addr.s_addr = htonl(0x7F000001);        /* 127.0.0.1 */
    /* Loopback refuses instantly when nothing is listening, so this probe was never the thing that
       stalled the console - but /api/health runs it on the accept loop several times per poll, and
       a raw connect() is a raw connect(). Bounded, so the rule holds everywhere with no exceptions
       to remember. */
    int rc = connect_deadline(s, (struct sockaddr *)&a, sizeof(a), CONNECT_MS);
    close(s);
    return rc == 0;
}

/* Our address on the LAN, so the UI can print a reachable ftp:// for the user.
   Discovered by asking the routing table which source address would be used to
   reach off-box; no packet is actually sent. Falls back to loopback.

   THE FALLBACK IS NEVER REMEMBERED, and that is the whole point of the second condition below.
   This used to memoise whatever it got on the FIRST call - including the "127.0.0.1" it writes
   before even attempting the lookup. The first call can easily come before the console has an
   IPv4 address: this payload is reloaded on every wake (rest/prepare exits the process and Payload
   Manager starts it again), and on a cold boot it runs while the console is still assembling
   itself. One early call and this function returned 127.0.0.1 for the entire life of the process -
   and that value is what /api/health, /api/network and /api/consoles publish as THIS CONSOLE'S
   ADDRESS. A companion on another PC reads it and has nowhere to send anything, which is exactly
   "the PS5 was not reachable" with the shop plainly running. Now only a real address is kept, so a
   later call simply asks again and the console heals itself the moment the interface is up.

   Locked because this is reached from the accept thread and from several workers. The only
   transition is fallback -> real, once. */
const char *lan_ip_str(void) {
    static char ip[24];
    static pthread_mutex_t ip_lock = PTHREAD_MUTEX_INITIALIZER;
    pthread_mutex_lock(&ip_lock);
    if (ip[0] && strcmp(ip, "127.0.0.1") != 0) { pthread_mutex_unlock(&ip_lock); return ip; }
    snprintf(ip, sizeof(ip), "127.0.0.1");
    int s = socket(AF_INET, SOCK_DGRAM, 0);
    if (s >= 0) {
        struct sockaddr_in a;
        memset(&a, 0, sizeof(a));
        a.sin_family = AF_INET;
        a.sin_port = htons(53);
        a.sin_addr.s_addr = htonl(0x08080808);    /* any off-net address will do */
        if (connect(s, (struct sockaddr *)&a, sizeof(a)) == 0) {
            struct sockaddr_in me;
            socklen_t ml = sizeof(me);
            if (getsockname(s, (struct sockaddr *)&me, &ml) == 0) {
                uint32_t v = ntohl(me.sin_addr.s_addr);
                if (v && v != 0x7F000001)
                    snprintf(ip, sizeof(ip), "%u.%u.%u.%u",
                             (v >> 24) & 0xFF, (v >> 16) & 0xFF, (v >> 8) & 0xFF, v & 0xFF);
            }
        }
        close(s);
    }
    pthread_mutex_unlock(&ip_lock);
    return ip;
}

/* Helper autostart can be turned off, and delayed; both are read in more than one place. */
#define AUTOSTART_OFF_FLAG      SHOP_DATA_DIR "/no-autostart"
#define AUTOSTART_DELAY_DEFAULT 30
#define AUTOSTART_DELAY_MAX     600
static int  autostart_disabled(void);
static int  autostart_delay_secs(void);
static void autostart_set_delay(int secs);

/* ---------------- stopping the homebrew payloads before rest mode -----------
 * Shutting only ourselves down was tested and the console still panicked on suspend,
 * so our process is not the thing that upsets it. The other loaded payloads keep
 * running services of their own — Elf Arsenal plus the dpiv2 / nanoDNS / ftpsrv
 * processes it spawns, GarlicSaves, ShadowMount — and none of them is part of the
 * console's sleep/wake cycle either.
 *
 * Payload Manager already exposes exactly what is needed, from the console itself:
 *   GET /processes_list          -> {"processes":[{"pid":N,"name":"dpiv2.elf",...}]}
 *   GET /process_kill?pid=N      -> {"ok":true,"message":"Killed"}
 *
 * This stops an EXPLICIT list of names and nothing else. An allow-list, never a
 * "kill everything it reports": that list may well include system processes, and
 * getting it wrong would take the console down harder than the crash we are chasing.
 * Whatever is left is reported back verbatim, so the real names can be read off a
 * live console and this list widened precisely instead of by guesswork.
 * -------------------------------------------------------------------------- */
static int name_has_ci(const char *hay, const char *needle) {
    if (!hay || !needle || !*needle) return 0;
    size_t nl = strlen(needle);
    for (const char *p = hay; *p; p++) {
        size_t i = 0;
        while (i < nl && p[i]) {
            char a = p[i], b = needle[i];
            if (a >= 'A' && a <= 'Z') a = (char)(a - 'A' + 'a');
            if (b >= 'A' && b <= 'Z') b = (char)(b - 'A' + 'a');
            if (a != b) break;
            i++;
        }
        if (i == nl) return 1;
    }
    return 0;
}

/* Elf Arsenal and the services it leaves behind, GarlicSaves, and ShadowMount.
   Arsenal's children outlive it, so each is named in its own right.

   `tile-autoinst` and `klogsrv` were added after a console rebuild left rest mode panicking
   again with everything else already stopped: they were the only homebrew still running through
   suspend. klogsrv in particular holds a kernel log socket open, which is exactly the kind of
   thing that does not survive a suspend cycle. Neither is part of the jailbreak — kstuff is, and
   it is deliberately absent from this list. */
static const char *REST_STOP[] = {
    "arsenal", "elf-arsenal", "elf_arsenal",
    "garlic", "garlicsaves",
    "dpiv2", "dpi.elf", "dpi_v2",
    "nanodns", "nano-dns", "nano_dns",
    "ftpsrv", "ftp-srv", "ftp_srv",
    "shadowmount", "shadow-mount", "shadow_mount",
    "tile-autoinst", "tile_autoinst",
    "klogsrv", "klog-srv",
    /* etaHEN is NOT ours and is not the install host (our installer is spawned per install and
       needs nothing running). It stays on this list only to stand down a copy the USER runs: it
       is exactly the class of long-lived, kernel-touching homebrew that made suspend panic
       before, and its util daemon also owns FTP and klog, so stopping it stops those with it. */
    "etahen", "eta-hen", "eta_hen",
    "onion_daemon", "onion_util",
};
#define REST_STOP_N ((int)(sizeof(REST_STOP) / sizeof(REST_STOP[0])))

/* Never touch these even if a name were to collide: Payload Manager is how anything
   gets loaded again, and the ELF loader is what put us here. */
/* Never stop these even if a name collides: Payload Manager is how anything gets loaded again and
   the ELF loaders are what put us here. "elfldr" already covers onion_elfldr by substring, but it
   is spelled out so a future edit to that list cannot silently strand the console. */
static const char *REST_KEEP[] = { "pldmgr", "payload_manager", "payloadmanager", "elfldr",
                                   "onion_elfldr" };
#define REST_KEEP_N ((int)(sizeof(REST_KEEP) / sizeof(REST_KEEP[0])))

static int rest_should_stop(const char *nm) {
    if (!nm || !nm[0]) return 0;
    for (int i = 0; i < REST_KEEP_N; i++) if (name_has_ci(nm, REST_KEEP[i])) return 0;
    for (int i = 0; i < REST_STOP_N; i++) if (name_has_ci(nm, REST_STOP[i])) return 1;
    return 0;
}

/* One look at Payload Manager's list, keeping only the processes we intend to stop. */
static int rest_scan(int *pids, char names[][64], int max, int *pm_ok) {
    long blen = 0;
    char *doc = http_get_ip("127.0.0.1", PLDMGR_PORT, "/processes_list", &blen);
    if (!doc) { *pm_ok = 0; return 0; }
    *pm_ok = 1;
    int n = 0, my_pid = (int)getpid(), depth = 0;
    const char *obj = NULL;
    const char *p = strstr(doc, "\"processes\"");
    if (!p) p = doc;
    for (; *p && n < max; p++) {
        if (*p == '{') { if (depth == 0) obj = p; depth++; continue; }
        if (*p == ']' && depth == 0) break;
        if (*p != '}') continue;
        depth--;
        if (depth != 0 || !obj) continue;
        /* BOUNDED TO THIS OBJECT. Unbounded, an entry with no "name" took the NEXT process's name
           while keeping its own pid - and rest_should_stop(name) is what decides whether to stop that
           pid. A decision and a target from two different processes is how you stop something nobody
           asked you to. `p` is this object's closing brace. */
        char nm[96] = {0};
        json_str_after_lim(obj, p, "name", nm, sizeof(nm));
        int pid = 0;
        const char *pp = strstr(obj, "\"pid\"");
        if (pp && pp < p) { pp = strchr(pp, ':'); if (pp && pp < p) pid = (int)strtol(pp + 1, NULL, 10); }
        else pp = NULL;
        obj = NULL;
        if (pid <= 0 || !nm[0] || pid == my_pid) continue;
        if (!rest_should_stop(nm)) continue;
        pids[n] = pid;
        snprintf(names[n], 64, "%s", nm);
        n++;
    }
    free(doc);
    return n;
}

/* Stop the homebrew payloads and then GO AND LOOK.

   Payload Manager answers {"ok":true,"message":"Killed"} whether or not the process actually
   died — verified on hardware: dpi.elf survives three consecutive kills, same pid each time,
   while every other payload dies on the first. The old code took that reply at face value and
   reported the process as stopped, so the app could say "safe to rest" with homebrew still
   running straight into suspend. Now: ask Payload Manager, signal it ourselves as root as well,
   re-read the list, and repeat. What is reported is what is genuinely gone. */
static size_t rest_stop_payloads(char *out, size_t outsz) {
    int  pids0[64], pids[64];
    char names0[64][64], names[64][64];
    int  pm_ok = 0;
    int  n0 = rest_scan(pids0, names0, 64, &pm_ok);
    if (!pm_ok) {
        return (size_t)snprintf(out, outsz,
            "{\"ok\":false,\"error\":\"Payload Manager did not answer on :%d\","
            "\"stopped\":[],\"stopped_count\":0,\"failed\":[],\"failed_count\":0,"
            "\"remaining\":[],\"remaining_count\":0}", PLDMGR_PORT);
    }
    for (int i = 0; i < n0; i++) { pids[i] = pids0[i]; snprintf(names[i], 64, "%s", names0[i]); }
    int n = n0;

    for (int pass = 0; pass < 3 && n > 0; pass++) {
        for (int i = 0; i < n; i++) {
            char q[80];
            snprintf(q, sizeof(q), "/process_kill?pid=%d", pids[i]);
            long kl = 0;
            char *kr = http_get_ip("127.0.0.1", PLDMGR_PORT, q, &kl);
            free(kr);
            kill((pid_t)pids[i], SIGKILL);   /* we are root; do not rely on the helper alone */
        }
        sleep(1);                            /* let them actually go */
        n = rest_scan(pids, names, 64, &pm_ok);
        if (!pm_ok) break;
    }

    /* Whatever is still in the list after all that is genuinely still running. */
    char stopped[1500], failed[900];
    size_t sl = 0, fl = 0;
    int sn = 0, fn = 0;
    stopped[0] = failed[0] = 0;
    for (int i = 0; i < n0; i++) {
        int still = 0;
        for (int j = 0; j < n; j++) if (pids[j] == pids0[i]) { still = 1; break; }
        char enm[200];
        json_escape(names0[i], enm, sizeof(enm));
        if (still) {
            if (fl < sizeof(failed) - 220)
                fl += (size_t)snprintf(failed + fl, sizeof(failed) - fl,
                                       "%s{\"pid\":%d,\"name\":\"%s\"}", fn ? "," : "", pids0[i], enm);
            fn++;
        } else {
            if (sl < sizeof(stopped) - 220)
                sl += (size_t)snprintf(stopped + sl, sizeof(stopped) - sl,
                                       "%s{\"pid\":%d,\"name\":\"%s\"}", sn ? "," : "", pids0[i], enm);
            sn++;
        }
    }
    return (size_t)snprintf(out, outsz,
        "{\"ok\":%s,\"stopped\":[%s],\"stopped_count\":%d,"
        "\"failed\":[%s],\"failed_count\":%d,\"remaining\":[%s],\"remaining_count\":%d}",
        fn == 0 ? "true" : "false", stopped, sn, failed, fn, failed, fn);
}

/* ---------------- filesystem API: how the app stops needing a third-party FTP -----------------
 *
 * The PC companion reads app.db, bgft.db, addcont.db and /user/appmeta through FTP, and FTP on this
 * console belongs to etaHEN (:1337) or to ftpsrv (:2121) - neither of which is ours. That is 18
 * call sites of dependency on software we do not ship, for the sake of reading files on a machine
 * where WE ALREADY RUN AS ROOT WITH AN HTTP SERVER.
 *
 * On security: this is not a new exposure, it is a smaller one. etaHEN's FTP already grants any
 * host on the network anonymous root access to the whole filesystem, with no origin check and no
 * way to switch it off without losing the install host. These routes sit behind the origin guard
 * added in 3.24.8. Once the companion no longer needs FTP at all, etaHEN's can be turned off.
 *
 * Paths must be absolute. There is deliberately no sandbox: the whole point is reading
 * /system_data/priv/mms/*.db and writing into ShadowMount's scan folders, which no sandbox that
 * would be worth having could permit anyway.
 * ------------------------------------------------------------------------------------------- */

/* Read the ?path= parameter, percent-decoded, and require it to be absolute. */
static int fs_param_path(const char *rawpath, char *out, size_t outsz) {
    char enc[1024] = {0};
    if (!qparam(rawpath, "path", enc, sizeof(enc))) return 0;
    url_decode(enc, out, outsz);
    return out[0] == '/';
}

/* Where mkdir and delete may act: the roots the package server already trusts (the drives, /data
   and its /user/data alias, our own data folder), which is every folder the companion ever creates
   or clears - a homebrew watch folder, the cheat library, a transfer probe. Reads and listings stay
   unbounded on purpose (the whole point of them is the console's databases under
   /system_data/priv/mms); creating and deleting have no business there, and an unauthenticated GET
   that could rmdir any path on a root process was an exposure with no user. */
static int fs_mutable_path_allowed(const char *p) {
    if (!p || p[0] != '/' || strstr(p, "..")) return 0;
    static const char *ROOTS[] = { "/mnt/usb", "/mnt/ext", "/data/", "/user/data/",
                                   SHOP_DATA_DIR "/", NULL };
    for (int i = 0; ROOTS[i]; i++) if (!strncmp(p, ROOTS[i], strlen(ROOTS[i]))) return 1;
    return 0;
}

static void fs_send_stat(int fd, const char *path) {
    struct stat st;
    if (stat(path, &st) != 0) {
        send_status(fd, "404 Not Found", "application/json",
                    "{\"ok\":false,\"error\":\"no such path\"}");
        return;
    }
    char esc[1100], o[1400];
    json_escape(path, esc, sizeof(esc));
    snprintf(o, sizeof(o),
             "{\"ok\":true,\"path\":\"%s\",\"dir\":%s,\"size\":%lld,\"mtime\":%lld}",
             esc, S_ISDIR(st.st_mode) ? "true" : "false",
             (long long)st.st_size, (long long)st.st_mtime);
    send_json(fd, o);
}

#define FS_LIST_MAX_BYTES ((size_t)8u << 20)
static void fs_send_list(int fd, const char *path) {
    DIR *d = opendir(path);
    if (!d) {
        send_status(fd, "404 Not Found", "application/json",
                    "{\"ok\":false,\"error\":\"cannot open directory\"}");
        return;
    }
    /* 1 MiB to start, growing to FS_LIST_MAX_BYTES. The fixed 96 KB this used to be held about
       1265 entries, and the cheat directories the companion syncs are bigger than that - so every
       listing of them came back `truncated`, the PC concluded thousands of files were missing, and
       re-sent them every 15 minutes for ever. `truncated` is still reported honestly at the cap,
       and `count` is the number of entries actually written, so a reader can tell. */
    size_t cap = 1u << 20, len = 0;
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
        size_t need = strlen(esc) + 128;
        if (len + need >= cap) {                                          /* never truncate mid-value */
            if (cap >= FS_LIST_MAX_BYTES) { truncated = 1; break; }
            size_t nc = cap * 2;
            if (nc > FS_LIST_MAX_BYTES) nc = FS_LIST_MAX_BYTES;
            char *nb = (char *)realloc(out, nc);
            if (!nb) { truncated = 1; break; }
            out = nb; cap = nc;
            if (len + need >= cap) { truncated = 1; break; }
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

/* Raw bytes of a file. Same streaming loop send_file() uses, but always octet-stream and never
   cached - these are live databases, and a cached bgft.db is a wrong install verdict. */
static void fs_send_read(int fd, const char *path) {
    int f = open(path, O_RDONLY);
    if (f < 0) {
        send_status(fd, "404 Not Found", "application/json",
                    "{\"ok\":false,\"error\":\"cannot open\"}");
        return;
    }
    struct stat st;
    if (fstat(f, &st) != 0 || !S_ISREG(st.st_mode)) {
        close(f);
        send_status(fd, "400 Bad Request", "application/json",
                    "{\"ok\":false,\"error\":\"not a regular file\"}");
        return;
    }
    char hdr[300];
    int hn = snprintf(hdr, sizeof(hdr),
                      "HTTP/1.1 200 OK\r\nContent-Type: application/octet-stream\r\n"
                      "Content-Length: %lld\r\nCache-Control: no-store\r\n"
                      "Access-Control-Allow-Origin: *\r\nConnection: close\r\n\r\n",
                      (long long)st.st_size);
    write_all(fd, hdr, (size_t)hn);
    char buf[65536];
    for (;;) {
        ssize_t r = read(f, buf, sizeof(buf));
        if (r <= 0) break;
        write_all(fd, buf, (size_t)r);
    }
    close(f);
}

/* Streamed upload. This is handled straight off the accept loop, BEFORE the generic POST path,
   because that one reads the whole body into an 8 KB stack buffer - fine for JSON, useless for a
   90 GB game. Writes to <path>.part and renames on success, so a broken transfer can never be
   mistaken for a finished file (and, in ShadowMount's watch folders, can never be auto-mounted
   half-written - see the partial-mount trap this project has already hit). */
static int fs_recv_write(int cl, const char *rawpath, const char *req, int header_len, int have) {
    char path[1024] = {0};
    if (!fs_param_path(rawpath, path, sizeof(path))) {
        send_status(cl, "400 Bad Request", "application/json",
                    "{\"ok\":false,\"error\":\"absolute ?path= required\"}");
        return 0;
    }
    long long want = 0;
    const char *cl_h = strcasestr_local(req, "content-length:");
    /* No Content-Length used to mean want == 0, and a header-less or chunked POST then "succeeded"
       by replacing whatever was at `path` with an EMPTY file - a cheat file, a config. The
       companion always sends the header, so this is refused, never guessed. A stated length of 0
       is a legitimate empty file and still goes through. */
    if (!cl_h) {
        send_status(cl, "411 Length Required", "application/json",
                    "{\"ok\":false,\"error\":\"content-length required\"}");
        return 0;
    }
    want = atoll(cl_h + 15);
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
    /* whatever of the body already arrived with the headers */
    if (have > 0) {
        if (write_all_checked(f, req + header_len, (size_t)have) != have) failed = 1;
        else got = have;
    }
    /* Heap, not stack: this now runs on a worker thread, and 64 KB is most of one of those. */
    char *buf = (char *)malloc(COPY_BUF_BYTES);
    if (!buf) failed = 1;
    while (!failed && got < want) {
        size_t chunk = COPY_BUF_BYTES;
        if ((long long)chunk > want - got) chunk = (size_t)(want - got);
        ssize_t r = read(cl, buf, chunk);
        if (r <= 0) { failed = 1; break; }
        if (write_all_checked(f, buf, (size_t)r) != r) { failed = 1; break; }
        got += r;
    }
    free(buf);
    close(f);

    if (failed || got != want) {
        unlink(part);
        char o[220];
        snprintf(o, sizeof(o),
                 "{\"ok\":false,\"error\":\"short write\",\"written\":%lld,\"expected\":%lld}",
                 got, want);
        send_status(cl, "500 Internal Server Error", "application/json", o);
        return 0;
    }
    unlink(path);                       /* rename() onto an existing file is not portable here */
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

/* /api/fs/write on its own thread, exactly as /pkgfile/ is served.
 *
 * It used to run inline on the accept loop, so a PC pushing a 90 GB game - or the cheat sync
 * sending a few thousand small files - froze the whole console API for the duration: health, the
 * UI, the install engine, the cheat engine. And the socket kept the accept loop's 8 s receive
 * timeout, so a PC that paused for longer than that (an AV scan, a disk spin-up, a Wi-Fi roam)
 * had its multi-GB upload cut off and reported as "the console rejected it". The worker owns the
 * socket, sets a receive timeout that fits a real transfer, and counts itself so rest mode can
 * tell an upload is in flight. The response JSON and the .part-then-rename are unchanged. */
typedef struct { int fd; int n; int hl; char path[1024]; char *req; } fswrite_t;
static int g_uploads_inflight = 0;
static pthread_mutex_t g_uploads_lock = PTHREAD_MUTEX_INITIALIZER;

static int uploads_in_flight(void) {
    pthread_mutex_lock(&g_uploads_lock);
    int n = g_uploads_inflight;
    pthread_mutex_unlock(&g_uploads_lock);
    return n;
}

static void *fswrite_thread(void *arg) {
    fswrite_t *j = (fswrite_t *)arg;
    struct timeval rcvto;
    rcvto.tv_sec = 120;                 /* a stalled PC gets two minutes, not eight seconds */
    rcvto.tv_usec = 0;
    setsockopt(j->fd, SOL_SOCKET, SO_RCVTIMEO, &rcvto, sizeof(rcvto));
    fs_recv_write(j->fd, j->path, j->req, j->hl, j->n - j->hl);
    close(j->fd);
    free(j->req);
    free(j);
    pthread_mutex_lock(&g_uploads_lock);
    g_uploads_inflight--;
    pthread_mutex_unlock(&g_uploads_lock);
    return NULL;
}

/* Returns 0 when the worker has taken the socket over; -1 means "serve it inline as before". */
static int fs_write_serve(int fd, const char *rawpath, const char *buf, int n, int hl) {
    fswrite_t *j = (fswrite_t *)calloc(1, sizeof(*j));
    if (!j) return -1;
    j->req = (char *)malloc((size_t)n + 1);
    if (!j->req) { free(j); return -1; }
    memcpy(j->req, buf, (size_t)n + 1);          /* headers plus whatever body arrived with them */
    j->fd = fd;
    j->n = n;
    j->hl = hl;
    snprintf(j->path, sizeof(j->path), "%s", rawpath);
    pthread_mutex_lock(&g_uploads_lock);
    g_uploads_inflight++;
    pthread_mutex_unlock(&g_uploads_lock);
    pthread_t t;
    pthread_attr_t attr;
    pthread_attr_init(&attr);
    pthread_attr_setstacksize(&attr, 256 * 1024);
    int rc = pthread_create(&t, &attr, fswrite_thread, j);
    pthread_attr_destroy(&attr);
    if (rc != 0) {
        pthread_mutex_lock(&g_uploads_lock);
        g_uploads_inflight--;
        pthread_mutex_unlock(&g_uploads_lock);
        free(j->req);
        free(j);
        return -1;
    }
    pthread_detach(t);
    return 0;
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
    /* now_ms_local() here, now_ms() in the PS4 copy - the two payloads spell their own clock
       helper differently and always have. It is the ONLY line that differs between them. */
    unsigned long long seed = (unsigned long long)now_ms_local();
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

/* The icon experiment's timing, on its own thread. It used to run inline in /api/notify: the
   plain toast, a 2 s pause and the probe, on the accept loop - so one probe request froze every
   API on this console (health, library, installs, cheats) for about three seconds. */
typedef struct { char txt[300]; int variant; } notify_probe_t;
static void *notify_probe_thread(void *arg) {
    notify_probe_t *p = (notify_probe_t *)arg;
    char lead[360];
    snprintf(lead, sizeof(lead),
             "Icon test %d\nIf this is the only line you see, that icon did not draw", p->variant);
    notify(lead);
    sleep(2);                       /* let the shell draw it before the second one */
    notify_icon_probe(p->txt, p->variant);
    free(p);
    return NULL;
}

/* Defined beside running_game(), where the two SceSystemService calls it is written against
   are declared. /api/health below needs it, so it is announced here rather than moving a
   definition past its own prototypes. */
static int running_title_cached(char *out, size_t outsz);

static void handle(int fd, const char *rawpath, const char *req) {
    char path[1024];
    snprintf(path, sizeof(path), "%s", rawpath);
    char *qs = strchr(path, '?'); if (qs) *qs = 0;    /* route without the ?query */

    if (!strcmp(path, "/api/register-pc")) {          /* /api/register-pc?ip=10.0.0.76&port=8710 */
        const char *ip = strstr(rawpath, "ip="), *pt = strstr(rawpath, "port=");
        if (ip && pt) {
            char ipb[64]; size_t j = 0; ip += 3;
            while (*ip && j < sizeof(ipb) - 1 && ((*ip >= '0' && *ip <= '9') || *ip == '.')) ipb[j++] = *ip++;
            ipb[j] = 0;
            int port = atoi(pt + 5);
            if (ipb[0] && port > 0 && port < 65536) {
                snprintf(g_pc_url, sizeof(g_pc_url), "http://%s:%d", ipb, port);  /* newest, for the UI */
                pc_register(ipb, port);     /* and remember ALL of them, so no PC is forgotten */
                /* A companion's version used to arrive ONLY inside a federation document, read
                   during a library merge - so while the peer block was served from cache it never
                   arrived at all. /api/pcs then reported every PC as version "", ver_cmp() had
                   nothing to compare, and the version ranking below fell back to "most recently
                   seen". That is how the console came to pick an OLDER companion over a newer one
                   right after a restart. The PC now states its version when it registers, every
                   8 seconds, independently of any cache. */
                const char *vq = strstr(rawpath, "ver=");
                if (vq) {
                    char vb[16]; size_t k = 0; vq += 4;
                    while (*vq && k < sizeof(vb) - 1 &&
                           ((*vq >= '0' && *vq <= '9') || *vq == '.')) vb[k++] = *vq++;
                    vb[k] = 0;
                    if (vb[0]) pc_set_version(ipb, port, vb);
                }
            }
        }
        send_json(fd, "{\"ok\":true}");
        return;
    }
    if (!strcmp(path, "/api/network")) {
        /* The Devices panel asks for this. Without it the console always claimed no PCs were
           found, even while it was merging their libraries. */
        char o[1200];
        size_t l = 0;
        l += snprintf(o + l, sizeof(o) - l,
                      "{\"this_pc\":{\"id\":\"console\",\"name\":\"This PS5\",\"lan_ip\":\"%s\","
                      "\"port\":%d,\"url\":\"http://%s:%d\",\"os\":\"PS5\",\"version\":\"" SHOP_VERSION "\","
                      "\"local\":true,\"count\":%d,\"counts\":{\"total\":%d}},\"peers\":[",
                      lan_ip_str(), PORT, lan_ip_str(), PORT,
                      g_local_title_count, g_local_title_count);
        pthread_mutex_lock(&g_pcs_lock);
        int f3 = 1;
        /* Each of these rows is printed with "online":true, which is a CLAIM rather than a reading -
           so a companion that stopped announcing itself stayed in the Devices card with a green
           light beside it for as long as the payload was loaded. */
        long long now_net = now_ms_local();
        for (int i = 0; i < PC_MAX && l < sizeof(o) - 200; i++) {
            if (!g_pcs[i].ip[0]) continue;
            if (now_net - g_pcs[i].last_ms > PC_STALE_MS) continue;
            l += snprintf(o + l, sizeof(o) - l,
                          "%s{\"id\":\"%s\",\"name\":\"%s\",\"lan_ip\":\"%s\","
                          "\"url\":\"http://%s:%d\",\"online\":true,"
                          "\"count\":%d,\"counts\":{\"total\":%d}}",
                          f3 ? "" : ",", g_pcs[i].ip,
                          g_pcs[i].name[0] ? g_pcs[i].name : g_pcs[i].ip, g_pcs[i].ip,
                          g_pcs[i].ip, g_pcs[i].port, g_pcs[i].count, g_pcs[i].count);
            f3 = 0;
        }
        pthread_mutex_unlock(&g_pcs_lock);
        snprintf(o + l, sizeof(o) - l, "],\"console\":null,\"scanning\":false}");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/network/scan")) {
        /* PCs announce themselves to us, so there is nothing to sweep for. Report what we have
           rather than pretending to scan. */
        int n2 = 0;
        pthread_mutex_lock(&g_pcs_lock);
        long long now_scan = now_ms_local();
        for (int i = 0; i < PC_MAX; i++)
            if (g_pcs[i].ip[0] && now_scan - g_pcs[i].last_ms <= PC_STALE_MS) n2++;
        pthread_mutex_unlock(&g_pcs_lock);
        char o[160];
        snprintf(o, sizeof(o), "{\"ok\":true,\"found\":%d,\"peers\":[]}", n2);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/pcs")) {
        /* Every companion that has announced itself to this console. */
        char o[900];
        size_t l = 0;
        l += snprintf(o + l, sizeof(o) - l, "{\"pcs\":[");
        pthread_mutex_lock(&g_pcs_lock);
        int f2 = 1;
        long long now_pcs = now_ms_local();
        for (int i = 0; i < PC_MAX && l < sizeof(o) - 120; i++) {
            if (!g_pcs[i].ip[0]) continue;
            if (now_pcs - g_pcs[i].last_ms > PC_STALE_MS) continue;
            char enm[96], evr[32];
            json_escape(g_pcs[i].name, enm, sizeof(enm));
            json_escape(g_pcs[i].ver, evr, sizeof(evr));
            l += snprintf(o + l, sizeof(o) - l,
                          "%s{\"ip\":\"%s\",\"port\":%d,\"name\":\"%s\",\"version\":\"%s\"}",
                          f2 ? "" : ",", g_pcs[i].ip, g_pcs[i].port, enm, evr);
            f2 = 0;
        }
        pthread_mutex_unlock(&g_pcs_lock);
        snprintf(o + l, sizeof(o) - l, "]}");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/companion")) {
        /* EVERY PC we know, not just the last one to announce itself.
           This handed the UI a single address held in a legacy variable, so if that particular
           machine happened to be off the console fell back to serving its own API — which can only
           install packages that are already on the PS5. One sleeping PC therefore broke installing
           from any of them, with a message about the console's own storage that made no sense for
           a game sitting on a PC. The UI tries these in order and takes the first that answers. */
        char out[900];
        size_t l = 0;
        pcpeer_t pcs[PC_MAX];
        pthread_mutex_lock(&g_pcs_lock);
        memcpy(pcs, g_pcs, sizeof(pcs));
        pthread_mutex_unlock(&g_pcs_lock);

        /* AND NOTHING THAT HAS GONE QUIET. This is the list the page walks to find a companion,
           and it spends four seconds on each entry before giving up on it - so a machine that has
           been off for days sitting at the front of it is the difference between a shop that opens
           and one that looks broken. PC_STALE_MS was honoured by every other reader of this table
           and not by this one, which is the list that actually reaches the page. The PS4 has always
           blanked them here; this is the same two lines. */
        {
            long long now_c = now_ms_local();
            for (int i = 0; i < PC_MAX; i++)
                if (pcs[i].ip[0] && (now_c - pcs[i].last_ms) > PC_STALE_MS)
                    pcs[i].ip[0] = 0;                 /* gone quiet - stop offering it */
        }

        /* NEWEST BUILD first, then most recently seen.

           This sorted on last_ms alone, and every companion re-announces every 8s, so which one the
           console picked was effectively a coin flip. Picking an OLD build meant the console UI
           drove that PC's engine - and an old build still has the Elf-Arsenal-shaped readiness
           probe (every install reads "dpi wedged") and the hardcoded payload_path("arsenal")
           recovery, which RELAUNCHES Elf Arsenal. Pressing Install on the console therefore
           resurrected Arsenal and restarted the :12800 port war. Observed on hardware with one PC
           on 3.19.0 and one on 3.20.2. */
        for (int a = 0; a < PC_MAX; a++)
            for (int b = a + 1; b < PC_MAX; b++) {
                if (!pcs[b].ip[0]) continue;
                int better;
                if (!pcs[a].ip[0]) better = 1;
                else {
                    int c = ver_cmp(pcs[b].ver, pcs[a].ver);
                    better = (c > 0) || (c == 0 && pcs[b].last_ms > pcs[a].last_ms);
                }
                if (better) { pcpeer_t t = pcs[a]; pcs[a] = pcs[b]; pcs[b] = t; }
            }

        l += snprintf(out + l, sizeof(out) - l, "{\"urls\":[");
        int f = 1;
        for (int i = 0; i < PC_MAX && l < sizeof(out) - 80; i++) {
            if (!pcs[i].ip[0]) continue;
            l += snprintf(out + l, sizeof(out) - l, "%s\"http://%s:%d\"",
                          f ? "" : ",", pcs[i].ip, pcs[i].port);
            f = 0;
        }
        l += snprintf(out + l, sizeof(out) - l, "],\"url\":");
        if (pcs[0].ip[0]) snprintf(out + l, sizeof(out) - l, "\"http://%s:%d\"}", pcs[0].ip, pcs[0].port);
        /* NOT g_pc_url AS A LAST RESORT ANY MORE. It holds the last address that ever announced
           itself and carries no timestamp, so when every entry above has gone quiet it put the very
           address the filter just dropped straight back in front of the page. "I know of no
           companion" is the true answer, and the page handles it - it stays on the console's own
           API, which serves the whole library. */
        else snprintf(out + l, sizeof(out) - l, "null}");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/credscan")) {
        /* Every live pid with its authid — so we can SEE which process holds installer-grade
           credentials instead of guessing at a constant. */
        char *buf = malloc(65536);
        if (!buf) { send_json(fd, "{\"ok\":false}"); return; }
        size_t len = 0;
        len += snprintf(buf + len, 65536 - len, "{\"ok\":true,\"procs\":[");
        int first = 1;
        for (int pid = 1; pid < 1200 && len < 60000; pid++) {
            uint64_t aid = kernel_get_ucred_authid(pid);
            if (!aid) continue;
            len += snprintf(buf + len, 65536 - len, "%s{\"pid\":%d,\"authid\":\"0x%llx\"}",
                            first ? "" : ",", pid, (unsigned long long)aid);
            first = 0;
        }
        snprintf(buf + len, 65536 - len, "]}");
        send_json(fd, buf);
        free(buf);
        return;
    }
    if (!strncmp(path, "/api/cred", 9)) {   /* read a pid's ucred authid/caps/attrs (diagnostic) */
        const char *pp = strstr(rawpath, "pid=");
        int pid = pp ? atoi(pp + 4) : (int)getpid();
        uint64_t authid = kernel_get_ucred_authid(pid);
        uint8_t caps[16], attrs[32]; char cs[40], as[70];
        kernel_get_ucred_caps(pid, caps);
        kernel_get_ucred_attrs(pid, attrs);
        for (int i = 0; i < 16; i++) sprintf(cs + i * 2, "%02x", caps[i]);
        for (int i = 0; i < 32; i++) sprintf(as + i * 2, "%02x", attrs[i]);
        char out[220];
        snprintf(out, sizeof(out), "{\"pid\":%d,\"authid\":\"0x%llx\",\"caps\":\"%s\",\"attrs\":\"%s\"}",
                 pid, (unsigned long long)authid, cs, as);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/quit")) {         /* hand :PORT over to a newly loaded build of ourselves */
        send_json(fd, "{\"ok\":true,\"bye\":true}");
        close(fd);
        _exit(0);
    }
    if (!strcmp(path, "/api/rest/prepare")) {
        /* Stop the other homebrew payloads, then ourselves. `self=0` keeps us running so the
           helper shutdown can be checked from a PC without losing the app at the same time. */
        int keep_self = 0;
        {
            char v[8] = {0};
            if (qparam(rawpath, "self", v, sizeof(v)) && (v[0] == '0' || v[0] == 'n'))
                keep_self = 1;
        }
        /* Never tear the console's services down underneath a running transfer. This used to
           _exit(0) mid-write. It checked only the download job, so a live move, a spawn hand-off
           and a PC upload were all torn down anyway - each of them a file still being written
           into a ShadowMount watch folder (they stage as .part now, so the leftover is a stray
           file rather than a mounted half-game, but the transfer is still lost). force=1 is the
           deliberate override, and the message says what is running rather than how to override
           it - "add force=1" is not something a person at a television can do. */
        {
            char fv[8] = {0};
            int forced = qparam(rawpath, "force", fv, sizeof(fv)) ? atoi(fv) : 0;
            const char *why = NULL;
            pthread_mutex_lock(&g_job_mtx);
            if (g_job.state == JOB_DOWNLOAD || g_job.state == JOB_INSTALL)
                why = "A download is still running - let it finish, then try rest mode again";
            pthread_mutex_unlock(&g_job_mtx);
            if (!why && spawn_lane_live())
                why = "An install is still being handed to the console - let it finish, then try "
                      "rest mode again";
            if (!why) {
                pthread_mutex_lock(&g_move_lock);
                if (g_move.active)
                    why = "A game is still being moved between drives - let it finish, then try "
                          "rest mode again";
                pthread_mutex_unlock(&g_move_lock);
            }
            if (!why && uploads_in_flight() > 0)
                why = "A file is still being sent to the console - let it finish, then try rest "
                      "mode again";
            if (why && !forced) {
                char o[600];
                snprintf(o, sizeof(o),
                    "{\"ok\":false,\"error\":\"busy\",\"message\":\"%s\",\"stopped\":[],"
                    "\"stopped_count\":0,\"failed\":[],\"failed_count\":0}", why);
                send_json(fd, o);
                return;
            }
        }

        char *out = (char *)malloc(6144);
        if (!out) { send_json(fd, "{\"ok\":false,\"error\":\"out of memory\"}"); return; }
        size_t l = rest_stop_payloads(out, 6000);
        int all_clear = (strstr(out, "\"ok\":true") != NULL);

        /* Say what actually happened. This used to announce "Rest mode should be safe now"
           unconditionally — including when Payload Manager never answered and nothing at all had
           been stopped, which is the worst possible moment to tell someone it is safe to sleep. */
        if (all_clear)
            notify_sync("All homebrew stopped\n"
                        "It is safe to put the console into rest mode now");
        else
            notify_sync("Some homebrew is still running\n"
                        "Resting now can crash the console - open the shop to see what survived");

        /* Fold in whether we are going too, then answer BEFORE exiting. */
        if (l > 1 && out[l - 1] == '}')
            l -= 1;
        /* Only stand down when the console really is clear. If something survived, staying up is
           what lets the app report it — our own process was never the thing that panicked. */
        int quitting = (!keep_self && all_clear);
        l += (size_t)snprintf(out + l, 6144 - l, ",\"self_quit\":%s}", quitting ? "true" : "false");
        send_json(fd, out);
        free(out);
        if (quitting) { close(fd); _exit(0); }
        return;
    }
    if (!strncmp(path, "/api/fs/", 8)) {
        const char *op = path + 8;
        char fp[1024] = {0};
        if (!fs_param_path(rawpath, fp, sizeof(fp))) {
            send_status(fd, "400 Bad Request", "application/json",
                        "{\"ok\":false,\"error\":\"absolute ?path= required\"}");
            return;
        }
        if (!strcmp(op, "read")) { fs_send_read(fd, fp); return; }
        if (!strcmp(op, "list")) { fs_send_list(fd, fp); return; }
        if (!strcmp(op, "stat")) { fs_send_stat(fd, fp); return; }
        if ((!strcmp(op, "mkdir") || !strcmp(op, "delete")) && !fs_mutable_path_allowed(fp)) {
            send_status(fd, "403 Forbidden", "application/json",
                        "{\"ok\":false,\"error\":\"that path is outside the folders the shop "
                        "may change\"}");
            return;
        }
        if (!strcmp(op, "mkdir")) {
            mkparents(fp);
            mkdir(fp, 0777);
            struct stat st;
            send_json(fd, (stat(fp, &st) == 0 && S_ISDIR(st.st_mode))
                          ? "{\"ok\":true}" : "{\"ok\":false,\"error\":\"mkdir failed\"}");
            return;
        }
        if (!strcmp(op, "delete")) {
            int rc = unlink(fp);
            if (rc != 0) rc = rmdir(fp);
            send_json(fd, rc == 0 ? "{\"ok\":true}"
                                  : "{\"ok\":false,\"error\":\"delete failed\"}");
            return;
        }
        send_status(fd, "404 Not Found", "application/json",
                    "{\"ok\":false,\"error\":\"unknown fs op\"}");
        return;
    }
    if (!strcmp(path, "/api/engine/spawn-cleanup")) {
        /* Called by the companion after every spawn install, success or failure. There is no
           daemon to clear and no port to unstick - this removes the handover files so the next
           install cannot possibly read the last one's request or verdict, and releases the busy
           latch. Safe to call at any time. */
        unlink(SPAWN_REQ_PATH);
        unlink(SPAWN_RES_PATH);
        spawn_installer_remove();     /* our engine leaves nothing behind in pldmgr's launcher */
        spawn_lane_release();
        ilog("install: cleaned up - ready for the next one");
        send_json(fd, "{\"ok\":true,\"cleaned\":true}");
        return;
    }
    if (!strcmp(path, "/api/engine/state")) {
        /* THE SETTINGS ENGINE PANEL, WITH NO PC IN THE ROOM.
         *
         * This was a companion-only endpoint. A PS5 serving its own page answered the unknown-/api/
         * stub {}, web/index.html treats a missing `state` as "no answer", and the panel fell to its
         * honest "Can't tell from here" with three dark LEDs - printed by the very console it could
         * not tell anything about, on a page that console was serving. The PS4 build has answered
         * this from the console for a release; this is the same answer for the lane a PS5 uses.
         *
         * NO `platform` KEY, deliberately: a console that does not name a platform is a PS5, which
         * is the rule /api/health and the page already follow. Saying it here would be the first
         * place in the project that contradicts it by naming the default.
         *
         * The busy read is COPIED FIELD FOR FIELD from /api/engine/spawn-status below rather than
         * re-derived, so the two routes cannot drift into disagreeing about the same latch. */
        int have_res2 = (access(SPAWN_RES_PATH, F_OK) == 0);
        pthread_mutex_lock(&g_spawn_lock);
        int  latched2 = g_spawn_busy;
        long started2 = g_spawn_started;
        pthread_mutex_unlock(&g_spawn_lock);
        int busy2 = latched2 && !have_res2 && (time(NULL) - started2) < SPAWN_STALE_SECS;
        long long busy_for2 = busy2 ? (long long)(time(NULL) - started2) : 0;
        int pld2 = port_open(PLDMGR_PORT);
        struct stat est;
        long long elogsz = (stat(SHOP_DATA_DIR "/install.log", &est) == 0)
                           ? (long long)est.st_size : 0;
        char eo[1100];
        snprintf(eo, sizeof(eo),
                 "{\"ok\":true,\"mode\":\"spawn\",\"ours\":true,"
                 "\"name\":\"PKG MUTANT SHOP engine\",\"shop_port\":%d,\"shop_ok\":true,"
                 "\"shop_version\":\"%s\",\"pldmgr_port\":%d,\"ready\":%s,"
                 "\"busy\":%s,\"busy_for\":%lld,\"log_bytes\":%lld,"
                 "\"console_ip\":\"%s\",\"on_console\":true,"
                 "\"state\":\"%s\",\"detail\":\"%s\",\"how\":\"%s\"}",
                 PORT, SHOP_VERSION, PLDMGR_PORT,
                 pld2 ? "true" : "false",
                 busy2 ? "true" : "false", busy_for2, elogsz, lan_ip_str(),
                 /* Same three words the page's own branches expect, and the same reasoning the PS4
                    route records: what is missing when Payload Manager is not answering is the
                    ENGINE's way to start, not the shop - which is plainly up, since it is serving
                    the page this is read on. */
                 busy2 ? "busy" : (pld2 ? "ready" : "engine-down"),
                 busy2 ? "An install is being handed to the console right now."
                       : (pld2 ? "Ready - installs can start straight away."
                               : "Payload Manager is not answering, and a new installer is started "
                                 "through it for every install. Load it again on the console."),
                 "A fresh installer is started for every install and exits once the console has "
                 "accepted the package. Nothing stays running, so nothing can wedge.");
        send_json(fd, eo);
        return;
    }
    if (!strcmp(path, "/api/engine/log")) {
        /* The console's own install history, newest run last. Plain text on purpose: it is meant
           to be read by a person who is trying to work out what happened. */
        if (send_file(fd, ILOG_PATH) != 0)
            send_status(fd, "200 OK", "text/plain", "no installs recorded yet\n");
        return;
    }
    if (!strcmp(path, "/api/engine/spawn-status")) {
        struct stat lst;
        long long logsz = (stat(SHOP_DATA_DIR "/install.log", &lst) == 0) ? (long long)lst.st_size : 0;
        /* busy_for, not `since`: a unix timestamp from the console is useless to a caller that does
           not know how far the console's clock has drifted, and every question anyone asks about
           this latch is really "how long has it been stuck". `since` stays for older callers. */
        /* Same rule as install-spawn: a verdict on disk means the job is finished, so report it
           as finished. `stale` says the latch was still set, which is worth seeing in a log even
           though it no longer blocks anything. */
        int have_res = (access(SPAWN_RES_PATH, F_OK) == 0);
        pthread_mutex_lock(&g_spawn_lock);
        int  latched = g_spawn_busy;
        long started = g_spawn_started;
        pthread_mutex_unlock(&g_spawn_lock);
        int really_busy = latched && !have_res && (time(NULL) - started) < SPAWN_STALE_SECS;
        long long busy_for = really_busy ? (long long)(time(NULL) - started) : 0;
        char o[300];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"busy\":%s,\"stale\":%s,\"since\":%lld,\"busy_for\":%lld,"
                 "\"has_result\":%s,\"log_bytes\":%lld}",
                 really_busy ? "true" : "false",
                 (latched && !really_busy) ? "true" : "false",
                 (long long)started, busy_for,
                 have_res ? "true" : "false", logsz);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/engine/install-spawn")) {
        /* THE BASE-GAME LANE. /api/engine/install-spawn?uri=<http url or local path>
           
           SOLVED 2026-08-25: sceAppInstUtilInstallByPackage returns 0x80B2116F when it is called
           from a payload INJECTED into a hijacked host process - which is what this ELF is. The
           identical call from a freshly SPAWNED process succeeds, with our ordinary homebrew
           authid. Credentials were never the issue (we matched etaHEN's entire ucred - authid,
           uids, jaildir, caps, attrs - and still got 0x80B2116F).
           
           So a base game is delegated to pms-installer.elf, which Payload Manager spawns as its own
           process. Add-ons still install in-process, because that already works and is faster.
           Proven: Castle Crashers (CUSA14409, absent beforehand) -> bgft 1036, title
           "PKG MUTANT SHOP", the five files on disk and real artwork in /user/appmeta. */
        char enc[1300] = {0}, uri[1300] = {0};
        if (!qparam(rawpath, "uri", enc, sizeof(enc))) {
            send_status(fd, "400 Bad Request", "application/json",
                        "{\"ok\":false,\"error\":\"uri required\"}");
            return;
        }
        /* ONE AT A TIME. Both installs would otherwise share installer-req.txt and the second
           would install whatever the first asked for. The latch expires so a spawned process that
           died without writing a result cannot block the queue for ever. A verdict on disk means
           the previous installer has already exited and releases the lane at once - both rules
           live in spawn_lane_claim(), under the lock, shared with the console-local lane. */
        long long token = 0;
        if (spawn_lane_claim(&token) != 0) {
            send_status(fd, "409 Conflict", "application/json",
                        "{\"ok\":false,\"busy\":true,\"error\":\"an install is already being "
                        "handed to the console - wait for it to finish\"}");
            return;
        }
        ilog("install: requested (async)  uri=%s  token=%lld", enc, token);
        url_decode(enc, uri, sizeof(uri));
        char fixed[1300];
        rewrite_for_install(uri, fixed, sizeof(fixed));

        /* Hand the request over and clear any previous verdict, so a stale result can never be
           read as this one's - the same rule the queue uses for bgft rows. */
        mkdir(SHOP_DATA_DIR, 0777);
        unlink(SPAWN_RES_PATH);
        int rq = open(SPAWN_REQ_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0777);
        if (rq < 0) {
            spawn_lane_release();      /* never leave the lane latched on a failure path */
            send_status(fd, "500 Internal Server Error", "application/json",
                        "{\"ok\":false,\"error\":\"The console would not let the shop write to "
                        "its own data folder - load the shop again from Payload Manager\"}");
            return;
        }
        {
            /* Line 2 is the display name. It is what the console files the download under and what
               the installer's toast says, so dropping it made every PC-driven install announce
               itself as "Your game". Line 3 is the token the installer echoes back. */
            char nenc[300] = {0}, nm[300] = {0};
            if (qparam(rawpath, "name", nenc, sizeof(nenc))) url_decode(nenc, nm, sizeof(nm));
            char line[1750];
            int ln = snprintf(line, sizeof(line), "%s\n%s\n%lld\n", fixed, nm, token);
            /* snprintf returns what it WOULD have written. Bounded today (1299+1+299+1+20+1 into
               1750) but one buffer edit away from making write() read past the stack object and
               put whatever follows it into the file the installer hands to ShellCore. */
            if (ln < 0) ln = 0;
            if ((size_t)ln > sizeof(line)) ln = (int)sizeof(line);
            ssize_t w = write(rq, line, (size_t)ln);
            (void)w;
        }
        close(rq);

        char reply[400] = {0};
        /* pldmgr's /loadpayload resolves by BASENAME against its OWN payload directory, so a
           full path elsewhere can silently run an older copy. Mirror ours into that directory
           first and load it from there - one location, no ambiguity. */
        /* ONCE. This block used to repeat spawn_installer_write()'s entire body inline, so the
           file pldmgr is about to execute was truncated and rewritten four times per request.
           Writing it from the bytes embedded in THIS ELF (which spawn_installer_write does) is
           what guarantees the installer always matches this build - the duplicate added nothing. */
        int wrote_installer = spawn_installer_write();
        if (wrote_installer != 0) {
            /* Refuse rather than spawn something we cannot vouch for. An incomplete ELF handed to
               elfldr is the one failure here with no upper bound on its consequences. */
            unlink(SPAWN_REQ_PATH);
            spawn_lane_release();
            ilog("install: ABORTED - the installer could not be written intact (rc=%d)",
                 wrote_installer);
            send_status(fd, "500 Internal Server Error", "application/json",
                        "{\"ok\":false,\"error\":\"The console would not accept our installer "
                        "onto its own storage - it may be out of space. Nothing was started.\"}");
            return;
        }
        int spawned = pm_get("/loadpayload:/data/pldmgr/payloads/pms-installer/pms-installer.elf");
        ilog("install: installer spawned  rc=%d", spawned);
        if (spawned != 0) {
            /* Release the lane. spawn_install_wait clears the latch on every failure path; this
               one did not, so a refusal from Payload Manager locked the queue out for 600s.
               Nothing was spawned, so the request and our copy in the launcher go too: a request
               left behind is what a hand-launched installer would run next. */
            unlink(SPAWN_REQ_PATH);
            spawn_installer_remove();
            spawn_lane_release();
            ilog("install: Payload Manager would not spawn the installer");
        }
        char eu[1400], o[1900];
        json_escape(fixed, eu, sizeof(eu));
        snprintf(o, sizeof(o),
                 "{\"ok\":%s,\"via\":\"spawned-process\",\"uri\":\"%s\","
                 "\"spawn_rc\":%d,\"note\":\"poll /api/engine/spawn-result for the verdict\"}",
                 spawned == 0 ? "true" : "false", eu, spawned);
        (void)reply;
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/engine/spawn-result")) {
        /* The spawned installer's verdict, verbatim. Absent = it has not finished (or never ran). */
        static char last_logged[2048];
        long long tok = g_spawn_token;
        int stale = 0;
        int rf = open(SPAWN_RES_PATH, O_RDONLY);
        if (rf >= 0) {
            /* Past the installer's out[1900] - the token is its last field, and a shorter read
               lost it behind a long URL and answered "no result yet" for ever. */
            char rb[2048] = {0};
            ssize_t rg = read(rf, rb, sizeof(rb) - 1);
            close(rf);
            /* A verdict that does not carry the token of the request THIS process issued is a
               late one from an earlier installer, and is reported as "no result yet" rather than
               handed to the companion as the answer to its job. While this process has issued no
               request at all (tok == 0: the shop was reloaded mid-install) there is nothing to
               compare against and the file is served exactly as before, so a companion already
               waiting on it still gets its answer. */
            if (rg > 0 && tok && !spawn_verdict_is_ours(rb, tok)) stale = 1;
            /* Log each verdict once - the companion polls this every couple of seconds. */
            if (rg > 0 && strcmp(rb, last_logged) != 0) {
                snprintf(last_logged, sizeof(last_logged), "%s", rb);
                ilog("install: %s  %s", stale ? "stale verdict ignored" : "verdict", rb);
            }
        }
        if (stale || send_file(fd, SPAWN_RES_PATH) != 0)
            send_status(fd, "404 Not Found", "application/json",
                        "{\"ok\":false,\"error\":\"no result yet\"}");
        return;
    }
    if (!strcmp(path, "/api/engine/procdiff")) {
        /* READ ONLY. Dumps every process/credential field the SDK can read, for any pid.
           WHY: measured on 2026-08-25, same package, same URI string, same second -
             ours     -> sceAppInstUtilInstallByPackage = 0x80B2116F
             etaHEN   -> SUCCESS
           Identical bytes in, different result out. So the arguments are eliminated and the
           difference is a property of the CALLING PROCESS. This endpoint exists to find which
           one, by diffing our process against etaHEN's util daemon field by field. */
        char ps[16] = {0};
        pid_t who = qparam(rawpath, "pid", ps, sizeof(ps)) ? (pid_t)atoi(ps) : getpid();
        uint8_t caps[16] = {0}, attrs[32] = {0};
        int caps_rc  = kernel_get_ucred_caps(who, caps);
        int attrs_rc = kernel_get_ucred_attrs(who, attrs);
        char caphex[40] = {0}, atthex[72] = {0};
        for (int i = 0; i < 16; i++) snprintf(caphex + i * 2, 3, "%02x", caps[i]);
        for (int i = 0; i < 32; i++) snprintf(atthex + i * 2, 3, "%02x", attrs[i]);
        char o[900];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"pid\":%d,\"self\":%d,"
                 "\"authid\":\"0x%016llx\","
                 "\"uid\":%d,\"ruid\":%d,\"svuid\":%d,\"rgid\":%d,\"svgid\":%d,"
                 "\"prison\":\"0x%llx\",\"rootdir\":\"0x%llx\",\"jaildir\":\"0x%llx\","
                 "\"proc\":\"0x%llx\",\"ucred\":\"0x%llx\",\"filedesc\":\"0x%llx\","
                 "\"caps\":\"%s\",\"caps_rc\":%d,\"attrs\":\"%s\",\"attrs_rc\":%d}",
                 (int)who, (int)getpid(),
                 (unsigned long long)kernel_get_ucred_authid(who),
                 (int)kernel_get_ucred_uid(who), (int)kernel_get_ucred_ruid(who),
                 (int)kernel_get_ucred_svuid(who), (int)kernel_get_ucred_rgid(who),
                 (int)kernel_get_ucred_svgid(who),
                 (unsigned long long)kernel_get_ucred_prison(who),
                 (unsigned long long)kernel_get_proc_rootdir(who),
                 (unsigned long long)kernel_get_proc_jaildir(who),
                 (unsigned long long)kernel_get_proc(who),
                 (unsigned long long)kernel_get_proc_ucred(who),
                 (unsigned long long)kernel_get_proc_filedesc(who),
                 caphex, caps_rc, atthex, attrs_rc);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/engine/authid")) {
        /* READ ONLY. Reports the authid of any pid (default: ours), so the credential etaHEN
           actually installs under is measured on this console instead of taken from a note.
           Writes nothing and calls nothing. */
        char ps[16] = {0};
        pid_t who = qparam(rawpath, "pid", ps, sizeof(ps)) ? (pid_t)atoi(ps) : getpid();
        unsigned long long a = kernel_get_ucred_authid(who);
        char o[220];
        snprintf(o, sizeof(o),
                 "{\"ok\":%s,\"pid\":%d,\"self\":%d,\"authid\":\"0x%016llx\"}",
                 a ? "true" : "false", (int)who, (int)getpid(), a);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/engine/install-inproc")) {
        if (!diagnostics_allowed()) {
            send_status(fd, "403 Forbidden", "application/json",
                        "{\"ok\":false,\"error\":\"engine diagnostics are off - create /data/pkg-mutant-shop/allow-diagnostics to enable them\"}");
            return;
        }
        /* Call InstallByPackage from THIS process — the real data install. */
        char uri[1200] = {0};
        if (!qparam(rawpath, "uri", uri, sizeof(uri)) || !uri[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"missing uri\"}");
            return;
        }
        char fixed[1300];
        rewrite_for_install(uri, fixed, sizeof(fixed));
        /* ?bigapp=0 skips the sceSystemServiceGetAppIdOfRunningBigApp() announcement. Diagnostic
           only; omitting the parameter keeps the long-standing behaviour. */
        char bg[8] = {0};
        int announce = qparam(rawpath, "bigapp", bg, sizeof(bg)) ? atoi(bg) : 1;
        /* &authid=0x4800000000000006 runs this one call as another process's credential and puts
           ours back afterwards. Omitting it keeps the long-standing behaviour exactly. */
        char au[32] = {0};
        unsigned long long authid = qparam(rawpath, "authid", au, sizeof(au))
                                    ? strtoull(au, NULL, 0) : 0ULL;
        char ct[12] = {0}, cp[12] = {0};
        int ctype = qparam(rawpath, "ctype", ct, sizeof(ct)) ? atoi(ct) : 0;
        int cplat = qparam(rawpath, "cplat", cp, sizeof(cp)) ? atoi(cp) : 0;
        /* &jail=0 clears jaildir for the call; &caps=<32 hex> sets the capability mask;
           &uids=N sets ruid/svuid/rgid/svgid. All restored afterwards, all default to no-op. */
        cred_profile_t credp;
        memset(&credp, 0, sizeof(credp));
        credp.authid = authid;
        char jv[8] = {0}, cv[40] = {0}, uv[8] = {0};
        if (qparam(rawpath, "jail", jv, sizeof(jv))) {
            credp.set_jaildir = 1;
            credp.jaildir_null = (atoi(jv) == 0);
        }
        if (qparam(rawpath, "caps", cv, sizeof(cv)) && strlen(cv) == 32) {
            unsigned char tmpc[16];
            if (hex2bytes(cv, tmpc, sizeof(tmpc)) == 16) {
                memcpy(credp.caps, tmpc, 16);
                credp.set_caps = 1;
            }
        }
        if (qparam(rawpath, "uids", uv, sizeof(uv))) {
            credp.set_uids = 1;
            credp.uid_val = atoi(uv);
        }
        char cid[64] = {0};
        int rc = install_by_package_inproc_ex2(fixed, cid, sizeof(cid), announce, ctype, cplat, &credp);
        char eu[1400], out[1700];
        json_escape(fixed, eu, sizeof(eu));
        snprintf(out, sizeof(out),
                 "{\"ok\":%s,\"rc\":\"0x%08X\",\"init_rc\":\"0x%08X\",\"via\":\"in-process\","
                 "\"announce_bigapp\":%d,\"ctype\":%d,\"cplat\":%d,"
                 "\"jail\":%d,\"caps_set\":%d,\"uids\":%d,"
                 "\"authid\":\"0x%016llx\",\"authid_now\":\"0x%016llx\","
                 "\"modules\":\"%s\",\"uri\":\"%s\",\"content_id\":\"%s\"}",
                 rc == 0 ? "true" : "false", (unsigned)rc, (unsigned)g_ai_init_rc, announce,
                 ctype, cplat,
                 credp.set_jaildir ? (credp.jaildir_null ? 0 : 1) : -1,
                 credp.set_caps, credp.set_uids ? credp.uid_val : -1,
                 authid, (unsigned long long)kernel_get_ucred_authid(getpid()),
                 g_preload_log, eu, cid);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/install-url")) {
        /* /api/engine/install-url?url=<http url>&name=<label>&dest=<dir>
           The in-process download-then-install lane. Nothing in the product calls it any more -
           the companion's install_pms() has no callers and the UI never did - and its install
           half is the in-process InstallByPackage that this project proved returns 0x80B2116F for
           a base game. It downloads any URL onto the system partition with no space check, so it
           is a diagnostic now, behind the same flag as the other hand-fired install routes. The
           fetch lane (/api/engine/fetch) is the product's download path and is untouched. */
        if (!diagnostics_allowed()) {
            send_status(fd, "403 Forbidden", "application/json",
                        "{\"ok\":false,\"error\":\"engine diagnostics are off - create /data/pkg-mutant-shop/allow-diagnostics to enable them\"}");
            return;
        }
        char url[1200] = {0}, name[160] = {0}, dest[400] = {0};
        if (!qparam(rawpath, "url", url, sizeof(url)) || strncmp(url, "http://", 7)) {
            send_json(fd, "{\"ok\":false,\"error\":\"need an http:// url\"}");
            return;
        }
        qparam(rawpath, "name", name, sizeof(name));
        if (!qparam(rawpath, "dest", dest, sizeof(dest)) || !dest[0])
            snprintf(dest, sizeof(dest), "%s", DL_DIR_DEFAULT);

        pthread_mutex_lock(&g_job_mtx);
        int busy = (g_job.state == JOB_DOWNLOAD || g_job.state == JOB_INSTALL);
        pthread_mutex_unlock(&g_job_mtx);
        if (busy) {
            send_json(fd, "{\"ok\":false,\"error\":\"a job is already running\"}");
            return;
        }

        /* filename from the URL tail, sanitised; keep .pkg */
        const char *tail = strrchr(url, '/');
        char base[200];
        snprintf(base, sizeof(base), "%s", (tail && tail[1]) ? tail + 1 : "download.pkg");
        char *q = strchr(base, '?'); if (q) *q = 0;
        for (char *p2 = base; *p2; p2++)
            if (!((*p2 >= 'a' && *p2 <= 'z') || (*p2 >= 'A' && *p2 <= 'Z') ||
                  (*p2 >= '0' && *p2 <= '9') || *p2 == '.' || *p2 == '_' || *p2 == '-'))
                *p2 = '_';
        if (!base[0]) snprintf(base, sizeof(base), "download.pkg");

        mkparents(dest); mkdir(dest, 0777);
        pthread_mutex_lock(&g_job_mtx);
        memset(&g_job, 0, sizeof(g_job));
        g_job.id = ++g_job_seq;
        g_job.state = JOB_DOWNLOAD;
        snprintf(g_job.url, sizeof(g_job.url), "%s", url);
        snprintf(g_job.path, sizeof(g_job.path), "%s/%s", dest, base);
        snprintf(g_job.name, sizeof(g_job.name), "%s", name[0] ? name : base);
        snprintf(g_job.msg, sizeof(g_job.msg), "Starting");
        char started[700];
        snprintf(started, sizeof(started), "%s", g_job.path);
        pthread_mutex_unlock(&g_job_mtx);

        pthread_t tid;
        pthread_attr_t dla;
    pthread_attr_init(&dla);
    pthread_attr_setstacksize(&dla, 256 * 1024);   /* deepest stack here: HTTP + install path */
    if (pthread_create(&tid, &dla, dl_worker, NULL) != 0) {
            job_set(JOB_ERROR, "could not start worker");
            send_json(fd, "{\"ok\":false,\"error\":\"could not start worker\"}");
            return;
        }
        pthread_detach(tid);
        char esc[760], out[900];
        json_escape(started, esc, sizeof(esc));
        snprintf(out, sizeof(out), "{\"ok\":true,\"started\":true,\"job_id\":%d,\"path\":\"%s\"}", g_job_seq, esc);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/fetch")) {
        /* GET /api/engine/fetch?url=<http url>&dest=<dir>&name=<exact file name>
           Download a file and put it somewhere. That is all it does.

           This is how a PS5 .ffpfsc backup reaches the console: our own downloader pulls it into
           the chosen drive's homebrew folder and ShadowMount mounts it. No install, no BGFT, no
           third-party daemon - and the name is preserved EXACTLY, unlike install-url, which
           sanitises it into underscores. */
        char url[1200] = {0}, dest[400] = {0}, enc[400] = {0}, name[400] = {0};
        /* https:// TOO, now that there is a TLS transport behind the same contract. This single
           strncmp was the whole reason the console could not fetch from a project's release page:
           everything past it - the free-space check, the write probe, .part-then-rename, the job
           progress, Cancel - was already right and already proven. */
        if (!qparam(rawpath, "url", url, sizeof(url)) ||
            (strncmp(url, "http://", 7) && strncmp(url, "https://", 8))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need an http:// or https:// url\"}");
            return;
        }
        if (!qparam(rawpath, "dest", dest, sizeof(dest)) || dest[0] != '/') {
            send_json(fd, "{\"ok\":false,\"error\":\"need an absolute dest folder\"}");
            return;
        }
        if (qparam(rawpath, "name", enc, sizeof(enc))) snprintf(name, sizeof(name), "%s", enc);
        if (!name[0]) {
            const char *tail = strrchr(url, '/');
            snprintf(name, sizeof(name), "%s", (tail && tail[1]) ? tail + 1 : "download.bin");
        }
        /* Keep spaces and brackets - ShadowMount reads these names and the user chose them. Refuse
           only what could escape the destination folder. */
        if (strchr(name, '/') || strstr(name, "..")) {
            send_json(fd, "{\"ok\":false,\"error\":\"that file name is not allowed\"}");
            return;
        }
        pthread_mutex_lock(&g_job_mtx);
        int busy = (g_job.state == JOB_DOWNLOAD || g_job.state == JOB_INSTALL);
        pthread_mutex_unlock(&g_job_mtx);
        if (busy) {
            send_json(fd, "{\"ok\":false,\"error\":\"a download is already running - "
                          "let it finish first\"}");
            return;
        }
        if (!strncmp(dest, "/mnt/", 5)) {
            /* Is that drive REALLY there? The write probe below passes on an unmounted
               /mnt/usbN just as well - it resolves to the parent filesystem - so a 100 GB
               backup was pulled onto the SYSTEM partition under a folder that looked like a
               drive, and vanished the moment a real one was plugged in. Same st_dev test
               /api/devices and /api/move use. */
            char droot[64];
            size_t k = 0;
            for (const char *p = dest + 5; *p && *p != '/' && k < sizeof(droot) - 6; p++) droot[k++] = *p;
            droot[k] = 0;
            char full[80];
            snprintf(full, sizeof(full), "/mnt/%s", droot);
            struct stat dstat, mstat;
            if (!k || stat(full, &dstat) != 0 ||
                (stat("/mnt", &mstat) == 0 && dstat.st_dev == mstat.st_dev)) {
                send_json(fd, "{\"ok\":false,\"error\":\"That drive is not connected\"}");
                return;
            }
        }
        mkparents(dest);
        mkdir(dest, 0777);
        {
            /* IS THERE ROOM? A PS5 backup is routinely 100 GB and the "internal" destination is
               /data/homebrew, which lives on the system partition - on this console that is 31 GB
               free against a 673 GB disk. Filling it is not a recoverable mistake, and finding out
               after an hour of transfer is not an answer. `size` is what the caller expects to
               write; when it is absent we skip the check rather than guess. */
            char sv[24] = {0};
            long long want = qparam(rawpath, "size", sv, sizeof(sv)) ? atoll(sv) : 0;
            struct statvfs vfs;
            if (want > 0 && statvfs(dest, &vfs) == 0) {
                long long freeb = (long long)vfs.f_bavail * (long long)vfs.f_frsize;
                /* 256 MB of headroom: a filesystem at absolute zero is a different kind of bad. */
                if (freeb < want + (256LL << 20)) {
                    char ed[420], o[760];
                    json_escape(dest, ed, sizeof(ed));
                    snprintf(o, sizeof(o),
                             "{\"ok\":false,\"error\":\"Not enough room on that drive - %s needs "
                             "%lld MB and %s has %lld MB free. Pick another drive.\","
                             "\"free\":%lld,\"need\":%lld}",
                             name, want >> 20, ed, freeb >> 20, freeb, want);
                    send_json(fd, o);
                    return;
                }
            }
        }
        {
            /* Prove we can actually write there BEFORE promising a multi-gigabyte transfer. A
               drive that is absent, full or read-only should say so in a second, not an hour. */
            char probe[720];
            snprintf(probe, sizeof(probe), "%s/.pms-write-test", dest);
            int pf = open(probe, O_WRONLY | O_CREAT | O_TRUNC, 0777);
            if (pf < 0) {
                char o[600];
                char ed[420];
                json_escape(dest, ed, sizeof(ed));
                snprintf(o, sizeof(o),
                         "{\"ok\":false,\"error\":\"Cannot write to %s - is that drive "
                         "connected?\"}", ed);
                send_json(fd, o);
                return;
            }
            close(pf);
            unlink(probe);
        }
        char dgst[72] = {0}, purp[16] = {0}, scb[8] = {0}, rtag[120] = {0};
        qparam(rawpath, "digest", dgst, sizeof(dgst));
        qparam(rawpath, "purpose", purp, sizeof(purp));
        qparam(rawpath, "tag", rtag, sizeof(rtag));
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
        pthread_mutex_lock(&g_job_mtx);
        memset(&g_job, 0, sizeof(g_job));
        g_job.id = ++g_job_seq;
        g_job.state = JOB_DOWNLOAD;
        g_job.fetch_only = 1;
        snprintf(g_job.digest, sizeof(g_job.digest), "%s", dgst);
        snprintf(g_job.purpose, sizeof(g_job.purpose), "%s", purp);
        snprintf(g_job.tag, sizeof(g_job.tag), "%s", rtag);
        g_job.stem_clean = qparam(rawpath, "stem_clean", scb, sizeof(scb)) ? atoi(scb) : 0;
        snprintf(g_job.url, sizeof(g_job.url), "%s", url);
        snprintf(g_job.final, sizeof(g_job.final), "%s/%s", dest, name);
        /* .part until the last byte lands: ShadowMount watches this folder and mounts whatever
           turns up, so a container that is still arriving would mount as a broken game. */
        snprintf(g_job.path, sizeof(g_job.path), "%s/%s.part", dest, name);
        snprintf(g_job.name, sizeof(g_job.name), "%s", name);
        snprintf(g_job.msg, sizeof(g_job.msg), "Starting");
        int jid = g_job.id;
        pthread_mutex_unlock(&g_job_mtx);
        ilog("fetch: requested  %s -> %s/%s", url, dest, name);

        pthread_t tid;
        pthread_attr_t fa;
        pthread_attr_init(&fa);
        pthread_attr_setstacksize(&fa, 256 * 1024);
        if (pthread_create(&tid, &fa, dl_worker, NULL) != 0) {
            pthread_attr_destroy(&fa);
            job_set(JOB_ERROR, "could not start the download");
            send_json(fd, "{\"ok\":false,\"error\":\"could not start the download\"}");
            return;
        }
        pthread_attr_destroy(&fa);
        pthread_detach(tid);
        char ef[700], o[900];
        json_escape(g_job.final, ef, sizeof(ef));
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"started\":true,\"job_id\":%d,\"path\":\"%s\","
                 "\"note\":\"poll /api/engine/job\"}", jid, ef);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/engine/lprobe")) {
        /* Diagnostic: send a line to a localhost port and return the reply. Used to talk to
           loopback-only install daemons that the PC cannot reach directly.

           This runs on the single accept loop, and it waited up to TEN MINUTES for a reply - so
           pointing it at any localhost port that accepts a connection and then says nothing
           (most request/response servers, waiting for input) took every API on this console
           down for that long. Nothing in the product calls it; it sits behind the diagnostics
           flag now, and the wait is bounded at 30 s. */
        if (!diagnostics_allowed()) {
            send_status(fd, "403 Forbidden", "application/json",
                        "{\"ok\":false,\"error\":\"engine diagnostics are off - create /data/pkg-mutant-shop/allow-diagnostics to enable them\"}");
            return;
        }
        char pbuf[16] = {0}, msg[1200] = {0};
        if (!qparam(rawpath, "port", pbuf, sizeof(pbuf)) ||
            !qparam(rawpath, "msg", msg, sizeof(msg))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need port and msg\"}");
            return;
        }
        int s2 = connect_local(atoi(pbuf), 30000, 15000);
        if (s2 < 0) { send_json(fd, "{\"ok\":false,\"error\":\"nothing listening\"}"); return; }
        write_all(s2, msg, strlen(msg));
        shutdown(s2, SHUT_WR);
        char rep[512] = {0};
        ssize_t n = read(s2, rep, sizeof(rep) - 1);
        close(s2);
        if (n > 0) { rep[n] = 0; for (ssize_t i = 0; i < n; i++) if (rep[i] < 32 || rep[i] > 126) rep[i] = ' '; }
        char er[600], out[900];
        json_escape(rep, er, sizeof(er));
        snprintf(out, sizeof(out), "{\"ok\":true,\"reply\":\"%s\"}", er);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/cheat/running")) {
        /* Everything the mods panel needs, discovered by US: which game is running, its pid and
           image base, and the best-matching cheat file for its exact version. */
        char tid[24] = {0};
        pid_t pid = 0;
        intptr_t base = 0;
        int rc = running_game(tid, sizeof(tid), &pid, &base);
        if (rc != 0) {
            char o[160];
            snprintf(o, sizeof(o), "{\"ok\":true,\"running\":false,\"rc\":%d}", rc);
            send_json(fd, o);
            return;
        }
        char ver[48] = {0};
        qparam(rawpath, "version", ver, sizeof(ver));
        char pick[600]; const char *why = "none";
        int exact = cheat_pick_file(tid, ver, pick, sizeof(pick), &why);
        char ef[700], o[1100];
        json_escape(pick, ef, sizeof(ef));
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"running\":true,\"title_id\":\"%s\",\"pid\":%d,\"base\":\"0x%llx\","
                 "\"cheat_file\":\"%s\",\"match\":\"%s\",\"exact\":%s}",
                 tid, (int)pid, (unsigned long long)base, ef, why,
                 exact ? "true" : "false");
        send_json(fd, o);
        return;
    }
    /* ---------------- companion-compatible API ---------------------------
     * The UI is ONE codebase served from both the PC and the console, so the
     * console has to answer the SAME endpoint names. Without these the mods
     * panel fell back to "cannot reach the mod engine" and the settings rows
     * showed dashes, even though everything it needed was running right here.
     * -------------------------------------------------------------------- */
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

        /* TWO VARIABLES, AND THE DIFFERENCE MATTERS. `iver` is what this console has installed: it
           feeds installed_version, the compatible test and patches_json, none of which may report
           what the caller ASKED for as though it were a fact about the console. `pver` is the version
           the picker selected, and all it chooses is which cheat file to read.
           48 bytes because a version is a FILENAME KEY, not a number - the longest in the shipped
           library is 29 characters ('01.03_ac3_engine_orbis_fn.elf'), and a clipped one resolves to
           no file at all. */
        char iver[48] = {0};
        title_t *trows = (title_t *)calloc(MAX_TITLES, sizeof(title_t));
        int tn = trows ? read_console_titles(trows, MAX_TITLES) : -1;
        for (int i = 0; i < tn; i++)
            if (!strcmp(trows[i].tid, tid)) { snprintf(iver, sizeof(iver), "%s", trows[i].ver); break; }
        free(trows);
        char pver[48] = {0};
        if (!qparam(rawpath, "version", pver, sizeof(pver))) pver[0] = 0;

        /* ?state=1 - the live on/off of each mod and nothing else, which is the refresh the panel
           fires after every toggle. The page has always sent it and this console has always ignored
           it. */
        char stq[8] = {0};
        int state_only = (qparam(rawpath, "state", stq, sizeof(stq)) && stq[0] && stq[0] != '0');

        char pick[600]; const char *why = "none";
        int exact = cheat_pick_file(tid, pver[0] ? pver : iver, pick, sizeof(pick), &why);
        if (pver[0] && !exact) {
            char ev[110], o[520];
            json_escape(pver, ev, sizeof(ev));
            snprintf(o, sizeof(o),
                     "{\"ok\":false,\"reachable\":true,\"title_id\":\"%s\","
                     "\"installed_version\":\"%s\",\"selected_version\":\"%s\","
                     "\"error\":\"no_cheat_file_for_version\",\"mods\":[],\"patches\":[],"
                     "\"candidates\":[]}", tid, iver, ev);
            send_json(fd, o);
            return;
        }
        if (!pick[0]) {
            /* No CHEAT file — but the title may still have game PATCHES, which live in a
               separate library. Dark Souls Remastered is exactly that case, and returning an
               empty patches[] here hid its patch completely. */
            size_t PSZ = 32768;
            char *pj = (char *)malloc(PSZ);
            if (pj) patches_json(tid, iver, pj, PSZ);
            int has_patch = pj && pj[0] == '[' && pj[1] != ']';
            size_t OSZ = PSZ + 400;
            char *o = (char *)malloc(OSZ);
            if (!o) { free(pj); send_json(fd, "{\"ok\":false,\"reachable\":true}"); return; }
            snprintf(o, OSZ,
                     "{\"ok\":%s,\"reachable\":true,\"engine\":\"mutant\","
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
        /* Live pid/base only when THIS title is the running game; otherwise the mods
           are listed read-only, because writing needs the live process. */
        char rtid[24] = {0};
        pid_t rpid = 0; intptr_t rbase = 0;
        int live = (running_game(rtid, sizeof(rtid), &rpid, &rbase) == 0 && !strcmp(rtid, tid));

        int non_json = 0;
        char *doc = cheat_load_doc(pick, &non_json);
        if (!doc) { send_json(fd, "{\"ok\":false,\"reachable\":true,\"error\":\"cannot read cheat file\"}"); return; }
        char fver[32] = {0};
        json_str_after(doc, "version", fver, sizeof(fver));
        const char *fmt = path_ext_is(pick, ".shn") ? "shn"
                        : path_ext_is(pick, ".mc4") ? "mc4" : "json";
        /* An unknown installed version is NOT a mismatch - only flag one we can prove. And NOT
           `exact`, which is exact against the version in the REQUEST: including it meant that
           deliberately picking another version reported compatible:true and the panel painted it
           green with "matched", hiding the mismatch note. The question is whether the file suits the
           game on the console, and only iver answers that. */
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
            "{\"ok\":true,\"reachable\":true,\"engine\":\"mutant\",\"title_id\":\"%s\","
            "\"installed_version\":\"%s\",\"selected_version\":\"%s\","
            "\"file_version\":\"%s\",\"file\":\"%s\","
            "\"path\":\"%s\",\"format\":\"%s\",\"compatible\":%s,\"reason\":\"%s\","
            "\"running\":%s,\"pid\":%d,\"base\":\"0x%llx\",\"mods\":[",
            tid, iver, pver, fver, eb, ep, fmt, compatible ? "true" : "false", reason,
            live ? "true" : "false", live ? (int)rpid : 0,
            (unsigned long long)(live ? rbase : 0));
        /* One walk of the document (next_mod_block) and the block goes to the state reader
           as-is: find_mod_block + cheat_mod_state per index rescanned the file from the top for
           every mod, on the accept loop. `dropped` is additive: entries the engine cannot read
           (see parse_mod_entries_ex) - such a mod's state is never "on". */
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
        /* INITIALISED, because state_only skips the call that fills it and it is formatted with %s
           straight into the reply - uninitialised stack there puts garbage inside "versions" and
           JSON.parse throws, blanking the panel. */
        char vers[600] = "[]";
        char *pj = NULL;
        /* Game patches used to be a hardcoded empty array here, so the 376-file patch library
           was invisible and the panel's patch rows never rendered. Sized to what is genuinely
           left in `out` — a title can carry 59 patches and must not run past the buffer. */
        if (!state_only) {
            cheat_versions_json(tid, vers, sizeof(vers));
            size_t left = (len + 800 < OUTSZ) ? (OUTSZ - len - 800) : 0;
            size_t PSZ = left > 24576 ? 24576 : left;
            pj = (PSZ > 64) ? (char *)malloc(PSZ) : NULL;
            if (pj) patches_json(tid, iver, pj, PSZ);
        }
        int m_rem = 0, m_has = cheat_master_info(doc, &m_rem);   /* see cheat_master_info */
        snprintf(out + len, OUTSZ - len,
                 "],\"patches\":%s,\"candidates\":[],\"master\":%s,\"master_removable\":%s,"
                 "\"versions\":%s}",
                 pj ? pj : "[]", m_has ? "true" : "false", m_rem ? "true" : "false", vers);
        send_json(fd, out);
        free(pj);
        free(out); free(ents); free(doc);
        return;
    }
    if (!strcmp(path, "/api/cheats/paths")) {              /* companion spelling */
        char o[1100];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"ftp\":\"ftp://%s:2121\",\"ftp_port\":2121,"
                 "\"drop_here\":\"%s\",\"json\":\"%s\",\"shn\":\"%s\",\"mc4\":\"%s\","
                 "\"patches\":\"%s\","
                 "\"also_watched\":[\"/data/cheats\",\"/mnt/usb0..usb7/cheats\"]}",
                 lan_ip_str(), CHEAT_INBOX_DIR, CHEAT_JSON_DIR, CHEAT_SHN_DIR,
                 CHEAT_MC4_DIR, CHEAT_PATCH_DIR);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheats/rescan")) {
        mkdir(CHEAT_INBOX_DIR, 0777);
        int filed = cheat_intake_all();
        char o[240];
        snprintf(o, sizeof(o), "{\"ok\":true,\"filed\":%d,\"json\":%d,\"shn\":%d,\"mc4\":%d}",
                 filed, count_dir(CHEAT_JSON_DIR), count_dir(CHEAT_SHN_DIR), count_dir(CHEAT_MC4_DIR));
        send_json(fd, o);
        if (filed > 0)
            notify_cheats_filed(filed);
        return;
    }
    if (!strcmp(path, "/api/sources")) {
        /* Honest: with no PC there are no download mirrors. The helper services ARE
           probed for real, so the settings LEDs mean something. */
        /* The header bar reads sources / best / local_paths. With none of them present it
           read "(none configured) · 0 sources", which looked like a fault. On the console the
           source IS the console: its own drives. */
        /* 10101, not 9021: 9021 is elfldr (always up), so this LED was green regardless of
           whether ShadowMount was running. /api/helpers was corrected in 3.24.x; this endpoint
           and /api/health were missed, and they are the two the settings panel actually reads. */
        int smp = port_open(SMP_API_PORT), dpi = port_open(12800), fport = ftp_live_port(), ftp = fport != 0;
        /* Every PC feeding us games is a source; listing only ourselves is what made the
           header read "1 source" no matter how many machines were connected. */
        char pcsrc[420] = {0};
        size_t pl = 0;
        pthread_mutex_lock(&g_pcs_lock);
        long long now_src = now_ms_local();
        for (int i = 0; i < PC_MAX && pl < sizeof(pcsrc) - 90; i++) {
            if (!g_pcs[i].ip[0]) continue;
            if (now_src - g_pcs[i].last_ms > PC_STALE_MS) continue;   /* "ok":true is a claim */
            pl += snprintf(pcsrc + pl, sizeof(pcsrc) - pl,
                           ",{\"name\":\"%s\",\"ok\":true,\"kind\":\"peer\",\"titles\":%d}",
                           g_pcs[i].name[0] ? g_pcs[i].name : g_pcs[i].ip, g_pcs[i].count);
        }
        pthread_mutex_unlock(&g_pcs_lock);
        char o[1300];
        snprintf(o, sizeof(o),
                 "{\"sources\":[{\"name\":\"this PS5\",\"ok\":true,\"latency_ms\":0,\"kind\":\"local\"}%s],"
                 "\"best\":\"on-console\","
                 "\"local_paths\":[\"%s\",\"/mnt/usb0..usb7\"],"
                 "\"helpers\":{\"shadowmount\":%s,\"shadowmount_port\":%d,"
                 "\"install_host\":%s,\"install_host_port\":12800,\"ftp\":%s,\"ftp_port\":%d},"
                 "\"on_console\":true}",
                 pcsrc, HOMEBREW_DIR,
                 smp ? "true" : "false", SMP_API_PORT, dpi ? "true" : "false",
                 ftp ? "true" : "false", fport);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/consoles")) {
        char o[320];
        /* ID AND PLATFORM, which this reply has never carried. Measured in the console's own
           browser with no PC awake: state.consoles held one entry reading
           "undefined:undefined@10.0.0.99", so state.consoleById was keyed on undefined and the
           panels that pick a console by id had nothing to pick. The PS4 build has always sent
           both; the page defaults a missing platform to PS5, which hid the omission here and
           would have been wrong on any other console. The PC companion sends the same two
           fields, so a page cannot tell which server answered. */
        snprintf(o, sizeof(o),
                 "{\"consoles\":[{\"id\":\"ps5\",\"name\":\"This PS5\",\"ip\":\"%s\","
                 "\"platform\":\"ps5\",\"online\":true,\"self\":true}]}", lan_ip_str());
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/install/status")) {
        pthread_mutex_lock(&g_linst_lock);
        char en[300], em[500], o[900];
        json_escape(g_linst.name, en, sizeof(en));
        json_escape(g_linst.msg, em, sizeof(em));
        snprintf(o, sizeof(o),
                 "{\"active\":%s,\"done\":%s,\"ok\":%s,\"accepted_only\":%s,"
                 "\"name\":\"%s\",\"message\":\"%s\"}",
                 g_linst.active ? "true" : "false", g_linst.done ? "true" : "false",
                 g_linst.ok ? "true" : "false", g_linst.accepted_only ? "true" : "false", en, em);
        pthread_mutex_unlock(&g_linst_lock);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/move/status")) {
        pthread_mutex_lock(&g_move_lock);
        char en[300], ee[260], o[900];
        json_escape(g_move.name, en, sizeof(en));
        json_escape(g_move.error, ee, sizeof(ee));
        int pct = (g_move.total > 0) ? (int)((g_move.copied * 100) / g_move.total) : 0;
        snprintf(o, sizeof(o),
                 "{\"active\":%s,\"done\":%s,\"ok\":%s,\"percent\":%d,"
                 "\"copied\":%lld,\"total\":%lld,\"name\":\"%s\",\"error\":\"%s\"}",
                 g_move.active ? "true" : "false", g_move.done ? "true" : "false",
                 g_move.ok ? "true" : "false", pct, g_move.copied, g_move.total, en, ee);
        pthread_mutex_unlock(&g_move_lock);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/devices")) {
        /* Every drive a game could be moved to, with real free space. */
        static const char *IDS[11]  = {"internal","ext0","ext1","usb0","usb1","usb2","usb3","usb4","usb5","usb6","usb7"};
        static const char *PTH[11]  = {"/user","/mnt/ext0","/mnt/ext1","/mnt/usb0","/mnt/usb1","/mnt/usb2",
                                       "/mnt/usb3","/mnt/usb4","/mnt/usb5","/mnt/usb6","/mnt/usb7"};
        static const char *LBL[11]  = {"Internal SSD","Extended (ext0)","Extended Storage","USB0","USB1","USB2",
                                       "USB3","USB4","USB5","USB6","USB7"};
        char o[2200];
        size_t l = 0;
        l += snprintf(o + l, sizeof(o) - l, "{\"ps5\":[");
        int firstd = 1;
        struct stat mnt_st;
        int have_mnt = (stat("/mnt", &mnt_st) == 0);
        /* EVERY destination is listed, with `detected` saying which ones are really there. Listing
           only what happens to be mounted meant the console offered two choices where the PCs
           offered eleven, so a stick you were about to plug in could not be picked at all. */
        for (int i = 0; i < 11 && l < sizeof(o) - 220; i++) {
            struct statvfs v;
            struct stat ds;
            int present = 1;
            long long tot = 0, fre = 0;
            if (stat(PTH[i], &ds) != 0) present = 0;
            else if (i >= 1 && have_mnt && ds.st_dev == mnt_st.st_dev) present = 0;  /* nothing plugged in */
            else if (statvfs(PTH[i], &v) != 0) present = 0;
            else {
                tot = (long long)v.f_blocks * (long long)v.f_frsize;
                fre = (long long)v.f_bavail * (long long)v.f_frsize;
                if (tot <= 0) present = 0;
            }
            if (present)
                l += snprintf(o + l, sizeof(o) - l,
                              "%s{\"id\":\"%s\",\"label\":\"%s\",\"free\":%lld,\"total\":%lld,\"detected\":true}",
                              firstd ? "" : ",", IDS[i], LBL[i], fre, tot);
            else
                l += snprintf(o + l, sizeof(o) - l,
                              "%s{\"id\":\"%s\",\"label\":\"%s\",\"detected\":false}",
                              firstd ? "" : ",", IDS[i], LBL[i]);
            firstd = 0;
        }
        snprintf(o + l, sizeof(o) - l, "]}");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/queue")) {
        /* One slot is enough here: the console installs a single package at a time. */
        pthread_mutex_lock(&g_linst_lock);
        int show = g_linst.held || g_linst.active || g_linst.done;
        char en[300], em[500], o[900];
        json_escape(g_linst.name, en, sizeof(en));
        json_escape(g_linst.msg, em, sizeof(em));
        const char *st = g_linst.held ? "held"
                       : g_linst.active ? "promoting"
                       : (g_linst.done ? (g_linst.ok ? "playable" : "error") : "queued");
        /* -1 means UNKNOWN, and the UI draws an indeterminate bar for it. This used to be a
           hard 50 for the entire install: on a 90 GB package that is twenty minutes of a bar that
           never moves, which is indistinguishable from a hang and is what makes someone
           power-cycle a console mid-write.

           We genuinely cannot know the figure in this process: the install runs in a SEPARATE
           spawned process (that separation is what makes installs work at all - see the installer
           notes), and it reports a verdict at the end rather than progress along the way. So the
           honest answer is "unknown", not a number that looks like measurement. The PC companion
           DOES know - it reads real byte counts out of bgft.db - and its queue row shows them. */
        int pct = g_linst.done ? (g_linst.ok ? 100 : 0) : (g_linst.active ? -1 : 0);
        if (show)
            snprintf(o, sizeof(o),
                     "{\"tasks\":[{\"id\":\"console-local\",\"name\":\"%s\",\"state\":\"%s\","
                     "\"pct\":%d,\"msg\":\"%s\",\"lane\":\"console-local\"}]}", en, st, pct, em);
        else
            snprintf(o, sizeof(o), "{\"tasks\":[]}");
        pthread_mutex_unlock(&g_linst_lock);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/queue/start")) {
        /* Release the held install. */
        pthread_mutex_lock(&g_linst_lock);
        int can = g_linst.held && !g_linst.active;
        if (can) { g_linst.held = 0; g_linst.active = 1; g_linst.done = 0;
                   snprintf(g_linst.msg, sizeof(g_linst.msg), "Installing"); }
        char nm2[240];
        snprintf(nm2, sizeof(nm2), "%s", g_linst.name);
        pthread_mutex_unlock(&g_linst_lock);
        if (!can) { send_json(fd, "{\"ok\":true,\"started\":0}"); return; }
        pthread_t it;
        pthread_attr_t ia;
        pthread_attr_init(&ia);
        pthread_attr_setstacksize(&ia, 256 * 1024);
        int irc = pthread_create(&it, &ia, localinst_thread, NULL);
        pthread_attr_destroy(&ia);
        if (irc != 0) {
            pthread_mutex_lock(&g_linst_lock);
            g_linst.active = 0; g_linst.held = 1;
            pthread_mutex_unlock(&g_linst_lock);
            send_json(fd, "{\"ok\":false,\"error\":\"could not start the install\"}");
            return;
        }
        pthread_detach(it);
        notifyf("Installing %s\nReading it straight from the drive - the console takes it from here",
                nm2);
        send_json(fd, "{\"ok\":true,\"started\":1}");
        return;
    }
    if (!strcmp(path, "/api/queue/clear")) {
        pthread_mutex_lock(&g_linst_lock);
        if (!g_linst.active && !g_linst.held) memset(&g_linst, 0, sizeof(g_linst));
        pthread_mutex_unlock(&g_linst_lock);
        send_json(fd, "{\"ok\":true}");
        return;
    }
    if (!strcmp(path, "/api/config")) {
        char o[440];
        snprintf(o, sizeof(o),
                 "{\"ps5_ip\":\"%s\",\"companion\":{\"port\":8710},"
                 "\"dpi\":{\"port_v2\":12800},\"on_console\":true,"
                 "\"library_paths\":[\"" CHEAT_ROOT "\"]}", lan_ip_str());
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheat/paths")) {
        /* Where everything lives, so files can be added from a PC over FTP. */
        char o[1100];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"ftp_port\":2121,"
                 "\"drop_here\":\"%s\","
                 "\"json\":\"%s\",\"shn\":\"%s\",\"mc4\":\"%s\",\"patches\":\"%s\","
                 "\"also_watched\":[\"/data/cheats\",\"/mnt/usb0..usb7/cheats\"],"
                 "\"note\":\"Any .json/.shn/.mc4 dropped in these folders is sorted "
                 "automatically by what is inside it, not by its file name.\"}",
                 CHEAT_INBOX_DIR, CHEAT_JSON_DIR, CHEAT_SHN_DIR, CHEAT_MC4_DIR, CHEAT_PATCH_DIR);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheat/rescan")) {
        /* Pick up files added since boot without needing a reload. */
        mkdir(CHEAT_INBOX_DIR, 0777);
        int filed = cheat_intake_all();
        char o[300];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"filed\":%d,\"json\":%d,\"shn\":%d,\"mc4\":%d}",
                 filed, count_dir(CHEAT_JSON_DIR), count_dir(CHEAT_SHN_DIR), count_dir(CHEAT_MC4_DIR));
        send_json(fd, o);
        if (filed > 0)
            notify_cheats_filed(filed);
        return;
    }
    if (!strcmp(path, "/api/cheat/library")) {
        /* What our own library holds, and whether the one-time migration has finished. */
        char o[600];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"root\":\"%s\",\"migrated\":%s,\"status\":\"%s\","
                 "\"json\":%d,\"shn\":%d,\"mc4\":%d,\"patches\":%d,"
                 "\"legacy\":{\"json\":%d,\"shn\":%d,\"mc4\":%d}}",
                 CHEAT_ROOT, g_lib_migrated ? "true" : "false", g_lib_status,
                 count_dir(CHEAT_JSON_DIR), count_dir(CHEAT_SHN_DIR),
                 count_dir(CHEAT_MC4_DIR), count_dir(CHEAT_PATCH_DIR),
                 count_dir(LEGACY_CHEAT_ROOT "/json"), count_dir(LEGACY_CHEAT_ROOT "/shn"),
                 count_dir(LEGACY_CHEAT_ROOT "/mc4"));
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheat/find")) {
        /* /api/cheat/find?title=<TID>[&version=<ver>] -> the cheat file we would use for that
           title, whether or not it is the game currently running. This is what lets the mods
           panel browse any installed title using OUR library only. */
        char tid[24] = {0}, ver[16] = {0};
        if (!qparam(rawpath, "title", tid, sizeof(tid)) || !tid[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"title required\"}");
            return;
        }
        /* A title id is the only thing we ever echo back into JSON here — keep it to the
           characters real ids use so a crafted query cannot break out of the string. */
        for (char *c = tid; *c; c++) {
            if (!((*c >= 'A' && *c <= 'Z') || (*c >= 'a' && *c <= 'z') ||
                  (*c >= '0' && *c <= '9') || *c == '-' || *c == '_')) { *c = 0; break; }
        }
        if (!tid[0]) { send_json(fd, "{\"ok\":false,\"error\":\"bad title\"}"); return; }
        qparam(rawpath, "version", ver, sizeof(ver));
        char pick[600]; const char *why = "none";
        int exact = cheat_pick_file(tid, ver, pick, sizeof(pick), &why);
        char ef[700], o[1000];
        json_escape(pick, ef, sizeof(ef));
        snprintf(o, sizeof(o),
                 "{\"ok\":%s,\"title_id\":\"%s\",\"cheat_file\":\"%s\","
                 "\"match\":\"%s\",\"exact\":%s}",
                 pick[0] ? "true" : "false", tid, ef, why, exact ? "true" : "false");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheat/list")) {
        /* /api/cheat/list?file=<path>  -> mods parsed straight out of the cheat file */
        char file[600] = {0};
        if (!qparam(rawpath, "file", file, sizeof(file)) || !file[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"need file\"}"); return;
        }
        char lp[16]={0}, lb[32]={0};
        qparam(rawpath,"pid",lp,sizeof(lp)); qparam(rawpath,"base",lb,sizeof(lb));
        int lpid = lp[0] ? atoi(lp) : 0;
        intptr_t lbase = lb[0] ? (intptr_t)strtoull(lb,NULL,0) : 0x400000;
        int non_json = 0;
        char *json = cheat_load_doc(file, &non_json);   /* .json / .shn / .mc4 */
        if (!json) { send_json(fd, "{\"ok\":false,\"error\":\"cannot read file\"}"); return; }
        char title[160]={0}, id[32]={0}, ver[32]={0}, proc[64]={0};
        json_str_after(json, "name", title, sizeof(title));
        json_str_after(json, "id", id, sizeof(id));
        json_str_after(json, "version", ver, sizeof(ver));
        json_str_after(json, "process", proc, sizeof(proc));
        const char *fmt = path_ext_is(file, ".shn") ? "shn"
                        : path_ext_is(file, ".mc4") ? "mc4" : "json";
        cheat_entry_t *ents = (cheat_entry_t *)malloc(CHEAT_ENTS_BYTES);
        size_t OUTSZ = 65536;                           /* 74 mods seen in one real file */
        char *out = malloc(OUTSZ);
        if (!out || !ents) { free(out); free(ents); free(json); send_json(fd, "{\"ok\":false}"); return; }
        char et[200], ei[64], ev[64], ep[128];
        json_escape(title, et, sizeof(et)); json_escape(id, ei, sizeof(ei));
        json_escape(ver, ev, sizeof(ev));   json_escape(proc, ep, sizeof(ep));
        size_t len = 0;
        len += snprintf(out+len, OUTSZ-len,
                        "{\"ok\":true,\"title\":\"%s\",\"id\":\"%s\",\"version\":\"%s\","
                        "\"process\":\"%s\",\"format\":\"%s\",\"mods\":[", et, ei, ev, ep, fmt);
        const char *from = mods_array_start(json);      /* one walk - see GET /api/mods/ */
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
            len += snprintf(out+len, OUTSZ-len,
                            "%s{\"index\":%d,\"name\":\"%s\",\"entries\":%d,\"dropped\":%d,"
                            "\"state\":\"%s\",\"on\":%s}",
                            i ? "," : "", i, esc2, n, dropped, stt,
                            strcmp(stt,"on")==0 ? "true" : "false");
        }
        char idbuf[24] = {0};
        snprintf(idbuf, sizeof(idbuf), "%s", id);
        char vers[600] = "[]";
        if (idbuf[0]) cheat_versions_json(idbuf, vers, sizeof(vers));
        /* A MASTER CODE CHANGES WHAT TURNING EVERYTHING OFF MEANS - see cheat_master_info. */
        int m_rem = 0, m_has = cheat_master_info(json, &m_rem);
        snprintf(out+len, OUTSZ-len, "],\"versions\":%s,\"master\":%s,\"master_removable\":%s}",
                 vers, m_has ? "true" : "false", m_rem ? "true" : "false");
        send_json(fd, out);
        free(out); free(ents); free(json);
        return;
    }
    if (!strcmp(path, "/api/patch/list")) {
        /* /api/patch/list?title=<TID>[&version=] — the game patches we hold for a title. */
        char tid[24] = {0}, ver[32] = {0};
        if (!qparam(rawpath, "title", tid, sizeof(tid))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need title\"}"); return;
        }
        qparam(rawpath, "version", ver, sizeof(ver));
        if (!ver[0]) {                      /* not told, so look it up ourselves */
            title_t *rows = (title_t *)calloc(MAX_TITLES, sizeof(title_t));
            int tn = rows ? read_console_titles(rows, MAX_TITLES) : -1;
            for (int i = 0; i < tn; i++)
                if (!strcmp(rows[i].tid, tid)) { snprintf(ver, sizeof(ver), "%s", rows[i].ver); break; }
            free(rows);
        }
        char file[600];
        int have = (patch_file_for(tid, file, sizeof(file)) == 0);
        size_t PSZ = 32768;
        char *pj = (char *)malloc(PSZ);
        if (!pj) { send_json(fd, "{\"ok\":false,\"error\":\"out of memory\"}"); return; }
        patches_json(tid, ver, pj, PSZ);
        size_t OSZ = PSZ + 800;
        char *o = (char *)malloc(OSZ);
        if (!o) { free(pj); send_json(fd, "{\"ok\":false,\"error\":\"out of memory\"}"); return; }
        char ef[700]; json_escape(have ? file : "", ef, sizeof(ef));
        snprintf(o, OSZ,
                 "{\"ok\":%s,\"title_id\":\"%s\",\"installed_version\":\"%s\",\"file\":\"%s\","
                 "\"patches\":%s}",
                 have ? "true" : "false", tid, ver, ef, pj);
        send_json(fd, o);
        free(o); free(pj);
        return;
    }
    if (!strcmp(path, "/api/patch/apply") || !strcmp(path, "/api/patch/revert")) {
        /* /api/patch/apply?title=&index=[&force=1][&dry=1]   (revert takes no dry) */
        char tid[24] = {0}, ib[16] = {0}, fb[8] = {0}, db[8] = {0};
        if (!qparam(rawpath, "title", tid, sizeof(tid))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need title\"}"); return;
        }
        qparam(rawpath, "index", ib, sizeof(ib));
        qparam(rawpath, "force", fb, sizeof(fb));
        qparam(rawpath, "dry", db, sizeof(db));
        char o[1400];
        patch_action_json(tid, ib[0] ? atoi(ib) : 0, fb[0] ? atoi(fb) : 0, db[0] ? atoi(db) : 0,
                          !strcmp(path, "/api/patch/revert"), o, sizeof(o));
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/cheat/apply")) {
        /* /api/cheat/apply?file=&mod=&on=0|1&pid=&base=0x400000[&force=1][&name=] */
        char file[600]={0}, mb[16]={0}, ob[8]={0}, pb[16]={0}, bb[32]={0}, fb[8]={0}, nm[160]={0};
        if (!qparam(rawpath,"file",file,sizeof(file)) || !qparam(rawpath,"pid",pb,sizeof(pb))) {
            send_json(fd,"{\"ok\":false,\"error\":\"need file and pid\"}"); return;
        }
        qparam(rawpath,"mod",mb,sizeof(mb)); qparam(rawpath,"on",ob,sizeof(ob));
        qparam(rawpath,"base",bb,sizeof(bb)); qparam(rawpath,"force",fb,sizeof(fb));
        qparam(rawpath,"name",nm,sizeof(nm));
        /* &check=1 RUNS THE WHOLE DECISION AND WRITES NOTHING. The reads, the gates and the reasons
           are identical to a real apply, so this cannot disagree with it - which is the point. */
        char ckb[8] = {0};
        qparam(rawpath, "check", ckb, sizeof(ckb));
        int check_only = ckb[0] ? atoi(ckb) : 0;
        int idx = mb[0]?atoi(mb):0, want = ob[0]?atoi(ob):1, force = fb[0]?atoi(fb):0;
        pid_t pid = (pid_t)atoi(pb);
        intptr_t base = bb[0] ? (intptr_t)strtoull(bb,NULL,0) : 0x400000;
        char detail[200] = {0};
        int rc = cheat_apply_mod(file, idx, want, pid, base, force, check_only,
                                 detail, sizeof(detail));
        /* Say what actually happened, to which game, in plain words. "written=N" is the
           number of memory patches that landed; a refusal is not a silent no-op. */
        char gtitle[24] = {0};
        pid_t gpid = 0; intptr_t gbase = 0;
        running_game(gtitle, sizeof(gtitle), &gpid, &gbase);
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
                /* WHAT "THE WHOLE MODULE" MEANS HERE. The base comes from running_game(), which measures it -
           the no-ASLR address is a last resort, not a default, exactly as on the PS4 side. The span is
           a guess on this console (nothing here reports a module size), so the reply says so and the
           gaps count tells the caller how much of it could not be read. */
        int span_guessed = 0;
        if (!spid || !sbase) {
            char rtid[24] = {0};
            pid_t apid = 0; intptr_t abase = 0;
            if (running_game(rtid, sizeof(rtid), &apid, &abase) == 1) {
                if (!spid) spid = apid;
                if (!sbase) sbase = abase;
            }
        }
        if (!sbase) sbase = 0x400000;
        if (sto <= 0) { sto = (long long)(64 << 20); span_guessed = 1; }
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
                 span_guessed ? "true" : "false",
                 why[0] ? ",\"error\":\"" : "", why[0] ? ew : "", why[0] ? "\"" : "");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/mem/read")) {
        /* /api/mem/read?pid=&addr=0x...&len=  -> hex. Proves the engine can see game memory. */
        char pb[16]={0}, ab[32]={0}, lb[16]={0};
        if (!qparam(rawpath,"pid",pb,sizeof(pb)) || !qparam(rawpath,"addr",ab,sizeof(ab))) {
            send_json(fd, "{\"ok\":false,\"error\":\"need pid and addr\"}"); return;
        }
        qparam(rawpath,"len",lb,sizeof(lb));
        int pid = atoi(pb);
        intptr_t addr = (intptr_t)strtoull(ab, NULL, 0);
        int len = lb[0] ? atoi(lb) : 16;
        if (len < 1) len = 1;
        if (len > 256) len = 256;
        unsigned char buf[256];
        int rc = mem_read((pid_t)pid, addr, buf, (size_t)len);
        char hex[520]; hex[0]=0;
        if (rc == 0) for (int i=0;i<len;i++) snprintf(hex+i*2, sizeof(hex)-i*2, "%02X", buf[i]);
        char out[700];
        snprintf(out, sizeof(out),
                 "{\"ok\":%s,\"rc\":%d,\"pid\":%d,\"addr\":\"0x%llx\",\"len\":%d,\"hex\":\"%s\"}",
                 rc==0?"true":"false", rc, pid, (unsigned long long)addr, len, hex);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/mem/write")) {
        /* /api/mem/write?pid=&addr=0x...&hex=..[&expect=..]
           expect= is a safety gate: the write only happens if what is there right now matches,
           so a cheat authored for another build can never scribble over the wrong code. */
        char pb[16]={0}, ab[32]={0}, hx[600]={0}, ex[600]={0};
        if (!qparam(rawpath,"pid",pb,sizeof(pb)) || !qparam(rawpath,"addr",ab,sizeof(ab))
            || !qparam(rawpath,"hex",hx,sizeof(hx))) {
            send_json(fd,"{\"ok\":false,\"error\":\"need pid, addr and hex\"}"); return;
        }
        qparam(rawpath,"expect",ex,sizeof(ex));
        int pid=atoi(pb);
        intptr_t addr=(intptr_t)strtoull(ab,NULL,0);
        unsigned char want[256], exp[256], cur[256];
        int n=hex2bytes(hx,want,sizeof(want));
        if (n<=0) { send_json(fd,"{\"ok\":false,\"error\":\"bad hex\"}"); return; }
        int verified=-1;
        if (ex[0]) {
            int en=hex2bytes(ex,exp,sizeof(exp));
            if (en!=n) { send_json(fd,"{\"ok\":false,\"error\":\"expect length mismatch\"}"); return; }
            if (mem_read((pid_t)pid,addr,cur,(size_t)n)!=0) {
                send_json(fd,"{\"ok\":false,\"error\":\"read failed\"}"); return;
            }
            verified = (memcmp(cur,exp,(size_t)n)==0);
            if (!verified) {
                char chex[520]; chex[0]=0;
                for (int i=0;i<n;i++) snprintf(chex+i*2,sizeof(chex)-i*2,"%02X",cur[i]);
                char o[700];
                snprintf(o,sizeof(o),"{\"ok\":false,\"error\":\"expect_mismatch\",\"current\":\"%s\"}",chex);
                send_json(fd,o); return;
            }
        }
        int rc=mem_write((pid_t)pid,addr,want,(size_t)n);
        char o[420];
        snprintf(o,sizeof(o),"{\"ok\":%s,\"rc\":%d,\"bytes\":%d,\"verified\":%d}",
                 rc==0?"true":"false",rc,n,verified);
        send_json(fd,o);
        return;
    }
    if (!strcmp(path, "/api/notify")) {      /* /api/notify?text=... -> on-screen toast */
        char txt[300] = {0};
        if (qparam(rawpath, "text", txt, sizeof(txt)) && txt[0]) {
            char icb[8] = {0};
            qparam(rawpath, "icon", icb, sizeof(icb));
            int variant = icb[0] ? atoi(icb) : 0;
            g_notify_rc = -12345; g_notify_rc_icon = -12345;
            int pending = 0;
            if (variant > 0) {
                /* PLAIN FIRST, ALWAYS. If the icon variant does not draw, the user still sees a
                   line telling them so - which is the whole point. Branding has been switched on
                   and reverted twice on the strength of a return code; the television decides.
                   The sequence runs on notify_probe_thread, not here (see it for why); the reply
                   says the codes are pending and the next call can read what landed. */
                notify_probe_t *p = (notify_probe_t *)calloc(1, sizeof(*p));
                if (p) {
                    snprintf(p->txt, sizeof(p->txt), "%s", txt);
                    p->variant = variant;
                    pthread_t t;
                    pthread_attr_t a;
                    pthread_attr_init(&a);
                    pthread_attr_setdetachstate(&a, PTHREAD_CREATE_DETACHED);
                    pthread_attr_setstacksize(&a, 128 * 1024);
                    if (pthread_create(&t, &a, notify_probe_thread, p) == 0) pending = 1;
                    else free(p);
                    pthread_attr_destroy(&a);
                }
                if (!pending) notify(txt);      /* no thread: at least say the words, plainly */
            } else {
                notify(txt);
            }
            /* Give the detached sender a moment so the caller can SEE the result. */
            if (!pending) for (int w = 0; w < 20 && g_notify_rc == -12345; w++) usleep(50000);
            char o[360];
            snprintf(o, sizeof(o),
                     "{\"ok\":%s,\"rc_plain\":%d,\"rc_icon\":%d,\"variant\":%d,\"pending\":%s,"
                     "\"uri\":\"%s\",\"note\":\"the television decides, not these numbers\"}",
                     (pending || g_notify_rc == 0) ? "true" : "false", g_notify_rc,
                     g_notify_rc_icon, variant, pending ? "true" : "false",
                     variant == 2 ? NOTIFY_ICON_SYSTEM :
                     variant == 3 ? NOTIFY_ICON_SHELL :
                     variant == 4 ? NOTIFY_ICON_FILEURI :
                     variant == 1 ? NOTIFY_ICON : "");
            send_json(fd, o);
            return;
        }
        send_json(fd, "{\"ok\":true}");
        return;
    }
    if (!strcmp(path, "/api/game/delete-backup")) {
        /* GET /api/game/delete-backup?tid=CUSA12345
           Remove a PS5 backup container from THIS CONSOLE so ShadowMount stops re-mounting it.

           Deleting the game from the dashboard does not do this: the container stays in the
           drive's homebrew folder, ShadowMount sees it on its next scan and the title reappears.

           SAFETY. The caller sends a TITLE ID, never a path. We find the container ourselves,
           inside HOMEBREW_ROOTS, and it must carry a backup extension and resolve to the title
           that was asked for. Nothing else on this console can be reached through this endpoint,
           and the PC's own library is not on this machine at all. */
        char tid[16] = {0};
        if (!qparam(rawpath, "tid", tid, sizeof(tid)) || !tid[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"which game? no title id was given\"}");
            return;
        }
        /* Refuse while it is running. Pulling the container out from under a mounted, running game
           is how you corrupt a save and hang the shell. */
        {
            /* running_game() returns 0 on SUCCESS (see its header at the bottom of this file), so
               this MUST be `== 0`. Written as a bare truth test it was inverted: a running game
               returned 0, the && short-circuited, and the guard was skipped in exactly the case it
               exists for. strcmp, not strstr - running_game normalises to a bare 9-character
               CUSA/PPSA id, so a substring test could only ever match the wrong title. */
            char rt[24] = {0};
            if (running_game(rt, sizeof(rt), NULL, NULL) == 0 && !strcmp(rt, tid)) {
                send_json(fd, "{\"ok\":false,\"error\":\"That game is running. Close it on the "
                              "PS5 first, then delete it.\"}");
                return;
            }
        }
        char bpath[512] = {0};
        long long bytes = 0;
        int is_dir = 0;
        if (!find_backup_for_tid(tid, bpath, sizeof(bpath), &bytes, &is_dir)) {
            send_json(fd, "{\"ok\":true,\"deleted\":false,\"already_gone\":true,"
                          "\"message\":\"There is no backup for this game on the console - "
                          "nothing to delete.\"}");
            return;
        }
        /* Look once more, with lstat, right before anything is removed. find_backup_for_tid()
           already refused a symlink; this is the same check at the moment it matters, so a link
           swapped in between the lookup and the delete is unlinked as a link, never followed. */
        {
            struct stat lst;
            if (lstat(bpath, &lst) != 0 || S_ISLNK(lst.st_mode)) {
                send_json(fd, "{\"ok\":false,\"error\":\"That backup changed while it was being "
                              "checked. Try again.\"}");
                return;
            }
        }
        /* Is the container mounted right now? ShadowMount leaves /user/app/<TID>/mount.lnk for a
           mounted backup. It is NOT a reason to refuse - deleting a registered-but-idle backup is
           the normal case this route exists for, and unlinking the file underneath a mount is safe
           (the space stays allocated until the mount is released). It IS a reason not to claim the
           bytes are freed and the game gone this instant, which the reply used to assert. */
        int mounted = 0;
        {
            char mlnk[128];
            snprintf(mlnk, sizeof(mlnk), "/user/app/%s/mount.lnk", tid);
            mounted = (access(mlnk, F_OK) == 0);
        }
        int rc;
        if (is_dir) {
            /* A folder-shaped dump. rmdir() alone cannot remove one - it only takes an EMPTY
               directory - so this needs the bounded recursive delete, which refuses to run outside
               a homebrew root. Measure it before removing it: a directory's own st_size is not the
               size of its contents. */
            bytes = tree_bytes(bpath, 0);
            rc = remove_tree(bpath, 0);
        } else {
            rc = unlink(bpath);
        }
        char eb[560], o[900];
        json_escape(bpath, eb, sizeof(eb));
        if (rc == 0) {
            ilog("delete: removed backup %s (%lld bytes, mounted=%d)", bpath, bytes, mounted);
            if (mounted)
                notifyf("%s removed from this console\nIt stays playable until the console "
                        "restarts, and its space is freed then - it will not come back", tid);
            else
                notifyf("%s removed from this console\nThe backup file is gone, so it will not come "
                        "back on the next scan", tid);
            snprintf(o, sizeof(o),
                     "{\"ok\":true,\"deleted\":true,\"path\":\"%s\",\"bytes\":%lld,"
                     "\"mounted\":%s,\"message\":\"%s\"}", eb, bytes,
                     mounted ? "true" : "false",
                     mounted ? "Removed from the console. It is still mounted, so its space is "
                               "freed when the console restarts. Delete it from the PS5 home "
                               "screen too if it is still showing there."
                             : "Removed from the console. Delete it from the PS5 home screen "
                               "too if it is still showing there.");
        } else {
            ilog("delete: FAILED to remove %s (errno %d)", bpath, errno);
            snprintf(o, sizeof(o),
                     "{\"ok\":false,\"path\":\"%s\",\"error\":\"The console would not delete "
                     "that %s. The drive may be read-only, or the game may still be mounted.\"}",
                     eb, is_dir ? "folder" : "file");
        }
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/open")) {
        /* Open the shop in the console's own browser. ?url= overrides, which is how a PC companion
           points the console at ITS library instead of ours. */
        char url[300] = {0};
        if (!qparam(rawpath, "url", url, sizeof(url)) || !url[0])
            snprintf(url, sizeof(url), "http://127.0.0.1:%d/", PORT);

        int (*launch)(const char *, int, int, int) = NULL;
        uint32_t h = 0;
        int hrc = kernel_dynlib_handle(-1, "libSceSystemService.sprx", &h);
        if (hrc == 0)
            launch = (int (*)(const char *, int, int, int))
                     kernel_dynlib_dlsym(-1, h, "sceSystemServiceLaunchWebBrowser");

        int lrc = -1;
        if (launch) lrc = launch(url, 0, 0, 0);

        /* The toast is not a consolation prize - it is the thing that works on every firmware,
           and it carries the address so the user can type it on a phone if the browser refuses. */
        if (launch && lrc == 0)
            notifyf("Opening PKG MUTANT SHOP\n%s", url);
        else
            notifyf("PKG MUTANT SHOP is running\nOpen it from Media, or %s in any browser", url);

        char eu[340], o[560];
        json_escape(url, eu, sizeof(eu));
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"launched\":%s,\"notified\":true,\"rc\":%d,"
                 "\"available\":%s,\"url\":\"%s\",\"message\":\"%s\"}",
                 (launch && lrc == 0) ? "true" : "false", lrc,
                 launch ? "true" : "false", eu,
                 (launch && lrc == 0)
                     ? "The console is opening the shop in its browser"
                     : "This firmware will not open the browser from a payload - the console was "
                       "told to show the address instead, or open the shop from Media");
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/power")) {
        /* The measurement readout for the power-state watcher. Read-only: this endpoint does not
           poll the flags itself, it just reports what the watcher thread has already seen, so
           calling it can never perturb what we are trying to measure. */
        size_t cap = 48000, len = 0;
        char *out = (char *)malloc(cap);
        if (!out) { send_json(fd, "{\"ok\":false,\"error\":\"oom\"}"); return; }
        pthread_mutex_lock(&g_pwr_lock);
        unsigned st = g_pwr_have[0] ? (unsigned)(g_pwr_val[0] & 0xFFFFULL) : 0;
        unsigned trig = g_pwr_have[0] ? (unsigned)((g_pwr_val[0] >> 32) & 0xFFFFULL) : 0;
        unsigned rcause = g_pwr_have[0] ? (unsigned)((g_pwr_val[0] >> 48) & 0xFFFFULL) : 0;
        len += (size_t)snprintf(out + len, cap - len,
            "{\"ok\":true,\"opened\":%d,\"state\":%u,\"state_name\":\"%s\","
            "\"trigger\":%u,\"reboot_cause\":%u,\"shellui_shutdown\":%s,\"events_total\":%d,"
            "\"flags\":[",
            g_pwr_opened, st, g_pwr_have[0] ? pwr_state_name(st) : "unknown", trig, rcause,
            (g_pwr_have[1] && (g_pwr_val[1] & PWR_SHELLUI_SHUTDOWN)) ? "true" : "false",
            g_pwr_log_n);
        for (int i = 0; i < 4; i++)
            len += (size_t)snprintf(out + len, cap - len,
                "%s{\"name\":\"%s\",\"open\":%s,\"have\":%s,\"value\":\"0x%016llx\",\"rc\":%d}",
                i ? "," : "", PWR_FLAG_NAMES[i], g_pwr_h[i] ? "true" : "false",
                g_pwr_have[i] ? "true" : "false", g_pwr_val[i], g_pwr_rc[i]);
        len += (size_t)snprintf(out + len, cap - len, "],\"events\":[");
        /* Oldest-first over whatever the ring still holds. */
        int total = g_pwr_log_n;
        int start = total > PWR_LOG_MAX ? total - PWR_LOG_MAX : 0;
        int wrote = 0;
        for (int k = start; k < total; k++) {
            pwr_ev_t *e = &g_pwr_log[k % PWR_LOG_MAX];
            char esc[360];
            json_escape(e->text, esc, sizeof(esc));
            if (len + strlen(esc) + 96 >= cap) break;      /* never truncate mid-value */
            len += (size_t)snprintf(out + len, cap - len,
                "%s{\"ms\":%lld,\"t\":%lld,\"text\":\"%s\"}",
                wrote ? "," : "", e->ms, e->real_s, esc);
            wrote++;
        }
        pthread_mutex_unlock(&g_pwr_lock);
        len += (size_t)snprintf(out + len, cap - len, "],\"events_returned\":%d}", wrote);
        send_json(fd, out);
        free(out);
        return;
    }
    if (!strcmp(path, "/api/helpers")) {
        /* Probed from the console itself: ShadowMount binds loopback-only, so the PC cannot see
           it and would always report it down. This is the authoritative answer.

           ShadowMount is on 10101, not 9021 - it announces its own listener in its log:
             [API] HTTP/JSON ready: http://127.0.0.1:10101/api/v1 (v1)
           9021 is elfldr, the ELF loader, which is always running, so probing it reported a
           permanent green light for the wrong process.

           etahen_ipc_open says only that etaHEN's jailbreak IPC port is LISTENING. Upstream
           documents it as localhost-only; on this build it also answers from the LAN, which the
           PC companion checks by connecting to it - that connection IS the test, and it is not
           something the console can determine about itself. We report it and never speak to it:
           the command set is unknown and it is a channel that jailbreaks processes. */
        char out[340];
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"shadowmount\":%s,\"shadowmount_port\":%d,"
                 "\"install_host\":%s,\"install_host_port\":12800,\"ftp\":%s,"
                 "\"ftp_port\":%d,"
                 "\"etahen_ipc_open\":%s,\"etahen_ipc_port\":9028}",
                 port_busy(SMP_API_PORT) ? "true" : "false", SMP_API_PORT,
                 port_busy(12800) ? "true" : "false",
                 ftp_live_port() != 0 ? "true" : "false",
                 ftp_live_port(),
                 port_busy(9028) ? "true" : "false");
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/payloads/autostart")) {
        /* /api/payloads/autostart?on=0|1 - when off, the shop starts no helper payloads and
           therefore triggers none of their startup notifications. */
        char v[8] = {0};
        if (qparam(rawpath, "on", v, sizeof(v))) {
            if (v[0] == '0') {
                int fd2 = open(AUTOSTART_OFF_FLAG, O_WRONLY | O_CREAT | O_TRUNC, 0644);
                if (fd2 >= 0) close(fd2);
            } else {
                unlink(AUTOSTART_OFF_FLAG);
            }
        }
        /* How long to wait after the console starts before touching the helpers. Loading them
           while the system is still coming up is what made waking from rest crash now and then. */
        char dv[8] = {0};
        if (qparam(rawpath, "delay", dv, sizeof(dv)) && dv[0])
            autostart_set_delay(atoi(dv));
        char o[220];
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"autostart\":%s,\"delay\":%d,\"delay_default\":%d,\"delay_max\":%d}",
                 autostart_disabled() ? "false" : "true",
                 autostart_delay_secs(), AUTOSTART_DELAY_DEFAULT, AUTOSTART_DELAY_MAX);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/payloads")) {
        /* TWO QUESTIONS, ONE ROUTE - AND THIS ROUTE ALREADY EXISTED. It reported what the bundled
           helpers did on boot (`bundled` and `status`, kept below so nothing that reads them
           breaks), and the Payloads & Homebrews panel needs what is true NOW. A second
           `if (!strcmp(path, "/api/payloads"))` added further down was simply unreachable - the
           first match wins - so both answers are assembled here.

           The catalogue itself is a static file this ELF already carries and the page reads it
           directly; parsing it in C to re-emit it would be a second implementation of the same
           file, and every one of those in this project has eventually disagreed with the first. */
        char live[760]; int ln = 0; live[0] = 0;
        for (int i = 0; i < PAYLOAD_BUNDLE_COUNT; i++) {
            const pb_entry_t *e = &PAYLOAD_BUNDLE[i];
            /* A PORT IS EVIDENCE, NOT THE ONLY EVIDENCE. Measured on this console: Payload Manager
               lists shadowmountplus.elf at a live pid while :10101 is closed, and kstuff and
               nanodns bind nothing a probe can reach at all. */
            /* Three ways a payload can prove it is up, and a thing that listens on UDP is
               only ever caught by the third. */
            int up = pm_running(e->filename)
                     || (e->port > 0 && !e->udp && port_busy(e->port))
                     || (e->port > 0 && e->udp && udp_port_taken(e->port));
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
        char have_p[900]; int hp = 0; have_p[0] = 0;
        for (int i = 0; i < PAYLOAD_BUNDLE_COUNT && hp < (int)sizeof(have_p) - 90; i++) {
            char fp[600];
            snprintf(fp, sizeof(fp), "%s/%s", PB_DIR, PAYLOAD_BUNDLE[i].filename);
            struct stat ps;
            if (stat(fp, &ps) != 0 || !S_ISREG(ps.st_mode)) continue;
            /* AND WHICH RELEASE IT IS, when this lane fetched it. Without a version the panel
               can only compare the catalogue's - the version the machine that BUILT the
               catalogue held - so an update taken here was offered again on every check. */
            char bv[160], ebv[200];
            dl_taken_version(PB_DIR, PAYLOAD_BUNDLE[i].filename, bv, sizeof(bv));
            json_escape(bv, ebv, sizeof(ebv));
            hp += snprintf(have_p + hp, sizeof(have_p) - (size_t)hp,
                           "%s{\"n\":\"%s\",\"s\":%lld,\"v\":\"%s\"}",
                           hp ? "," : "", PAYLOAD_BUNDLE[i].filename, (long long)ps.st_size, ebv);
        }
        /* AND WHAT THE CONSOLE DOWNLOADED ITSELF - every file in the folder this build does not
           carry a copy of. The bundled ones are answered from the bundle above, which is the rule
           that stops a leftover from an older build winning a comparison. */
        for (int k = 0; k < 24 && hp < (int)sizeof(have_p) - 90; k++) {
            char dn[300];
            long long dsz = 0;
            if (!pb_downloaded_find(NULL, dn, sizeof(dn), &dsz, k)) break;
            char edn[320], dv[160], edv[200];
            json_escape(dn, edn, sizeof(edn));
            dl_taken_version(PB_DIR, dn, dv, sizeof(dv));
            json_escape(dv, edv, sizeof(edv));
            hp += snprintf(have_p + hp, sizeof(have_p) - (size_t)hp,
                           "%s{\"n\":\"%s\",\"s\":%lld,\"v\":\"%s\"}",
                           hp ? "," : "", edn, dsz, edv);
        }
        char apps[3200];
        app_ids_json(apps, sizeof(apps));
        /* BIG ENOUGH FOR ITS PARTS, WHICH out[2600] WAS NOT. This reply carries live[760] +
           have[1400] + apps[3200] + have_p[900] + esc2[400] plus the JSON around them - close to
           6.7 KB at worst - and snprintf truncates silently. It did: adding shop_version pushed the
           PS5's answer to exactly 2599 bytes and the page got invalid JSON, which is the same shape
           of failure a 900-byte buffer once caused by cutting a 117-entry title list off at 72.
           Sized from the parts, and the truncation is reported rather than left to be discovered. */
        char esc2[400], out[8192];
        json_escape(g_pb_log, esc2, sizeof(esc2));
        int _n = snprintf(out, sizeof(out),
                 "{\"ok\":true,\"on_console\":true,\"platform\":\"PS5\",\"console\":\"%s\","
                 /* THE BUILD THAT IS ANSWERING. Our own tile used to show the version recorded in
                    the catalogue baked into this ELF - which is the version the OWNER'S FOLDER
                    held when this ELF was built, and is therefore always one release behind by
                    construction. The console saying "Running - 3.86.0" while running 3.87.0 is
                    that loop, and it also made the panel offer an update that was already
                    installed. A running program knows its own version; nothing else does. */
                 "\"shop_version\":\"" SHOP_VERSION "\","
                 "\"source_here\":false,\"items\":null,\"hb_dir\":\"%s\","
                 "\"bundled\":%d,\"status\":\"%s\","
                 "\"state\":{\"live\":[%s],\"kept\":[%s],\"apps\":[%s],\"have\":[%s]}}",
                 console_id(), HB_DIR, PAYLOAD_BUNDLE_COUNT, esc2, live, have, apps, have_p);
        if (_n >= (int)sizeof(out))
            ilog("payloads: reply needed %d bytes but the buffer is %d - it was truncated",
                 _n, (int)sizeof(out));
        send_json(fd, out);
        return;
    }
    /* TWO NAMES, ONE ANSWER. /api/download/status is the name the page uses, because the PS4's
       /api/engine/job is about its install/spawn job and answers a different shape - so the page
       can poll a download without asking which console it is talking to. The old name keeps
       working: the companion has read it for a long time. */
    if (!strcmp(path, "/api/engine/job") ||
        !strcmp(path, "/api/download/status")) {     /* live progress for the UI */
        pthread_mutex_lock(&g_job_mtx);
        dl_job_t j = g_job;
        pthread_mutex_unlock(&g_job_mtx);
        const char *st = j.state == JOB_DOWNLOAD ? "downloading"
                       : j.state == JOB_INSTALL  ? "installing"
                       : j.state == JOB_DONE     ? "done"
                       : j.state == JOB_ERROR    ? "error" : "idle";
        int pct = (j.total > 0) ? (int)((j.done * 100) / j.total) : (j.state == JOB_DONE ? 100 : 0);
        char en[200], em[260], out[900];
        json_escape(j.name, en, sizeof(en));
        json_escape(j.msg, em, sizeof(em));
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"job_id\":%d,\"state\":\"%s\",\"pct\":%d,\"done\":%lld,\"total\":%lld,"
                 "\"name\":\"%s\",\"msg\":\"%s\",\"content_id\":\"%s\",\"rc\":\"0x%08X\"}",
                 j.id, st, pct, j.done, j.total, en, em, j.content_id, (unsigned)j.rc);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/cancel") || !strcmp(path, "/api/download/cancel")) {
        pthread_mutex_lock(&g_job_mtx);
        g_job.cancel = 1;
        pthread_mutex_unlock(&g_job_mtx);
        send_json(fd, "{\"ok\":true}");
        return;
    }
    if (!strcmp(path, "/api/engine/diag")) {
        if (!diagnostics_allowed()) {
            send_status(fd, "403 Forbidden", "application/json",
                        "{\"ok\":false,\"error\":\"engine diagnostics are off - create /data/pkg-mutant-shop/allow-diagnostics to enable them\"}");
            return;
        }
        /* Compare install paths side by side so we stop guessing: plain in-process call vs
           the same call under SceShellCore's borrowed credentials. */
        char uri[1200] = {0}, pbuf[16] = {0};
        if (!qparam(rawpath, "uri", uri, sizeof(uri)) || !uri[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"missing uri\"}");
            return;
        }
        int cred_pid = qparam(rawpath, "cred_pid", pbuf, sizeof(pbuf)) ? atoi(pbuf) : 0;
        char fixed[1300];
        rewrite_for_install(uri, fixed, sizeof(fixed));

        char cid1[64] = {0}, cid2[64] = {0};
        int rc_plain = install_by_package_inproc(fixed, cid1, sizeof(cid1));
        int used = 0;
        int rc_cred = install_full(fixed, cred_pid, cid2, sizeof(cid2), &used);
        int sc = find_pid_by_authid(AUTHID_SHELLCORE, 900);

        char eu[1400], out[1900];
        json_escape(fixed, eu, sizeof(eu));
        snprintf(out, sizeof(out),
                 "{\"ok\":true,\"uri\":\"%s\",\"shellcore_pid\":%d,\"cred_pid_used\":%d,"
                 "\"rc_plain\":\"0x%08X\",\"cid_plain\":\"%s\","
                 "\"rc_shellcore\":\"0x%08X\",\"cid_shellcore\":\"%s\"}",
                 eu, sc, used, (unsigned)rc_plain, cid1, (unsigned)rc_cred, cid2);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/sym")) {
        /* Which symbols does libSceAppInstUtil really export on THIS firmware?
           Resolving by NID means a wrong hash is indistinguishable from a missing function, so
           this asks by name and reports the address, and lets a NID be tried too. Read-only:
           nothing is called, only looked up. */
        char nm[128] = {0}, nid[32] = {0}, mod[96] = {0};
        qparam(rawpath, "name", nm, sizeof(nm));
        qparam(rawpath, "nid", nid, sizeof(nid));
        if (!qparam(rawpath, "mod", mod, sizeof(mod)) || !mod[0])
            snprintf(mod, sizeof(mod), "libSceAppInstUtil.sprx");
        appinst_once();
        uint32_t h = 0;
        int hrc = kernel_dynlib_handle(-1, mod, &h);
        unsigned long long a_name = 0, a_nid = 0;
        if (hrc == 0 && nm[0])  a_name = (unsigned long long)kernel_dynlib_dlsym(-1, h, nm);
        if (hrc == 0 && nid[0]) a_nid  = (unsigned long long)kernel_dynlib_resolve(-1, h, nid);
        char o[520];
        snprintf(o, sizeof(o),
                 "{\"ok\":%s,\"module\":\"%s\",\"handle_rc\":%d,\"handle\":%u,"
                 "\"name\":\"%s\",\"addr_by_name\":\"0x%llx\","
                 "\"nid\":\"%s\",\"addr_by_nid\":\"0x%llx\",\"preload\":\"%s\"}",
                 (a_name || a_nid) ? "true" : "false", mod, hrc, (unsigned)h,
                 nm, a_name, nid, a_nid, g_preload_log);
        send_json(fd, o);
        return;
    }
    /* ---------------------------------------------------- Payloads & Homebrews ---------------
     * THE PANEL HAS TO WORK WITH NO PC. The catalogue is a static file this ELF already carries
     * (web/assets/payloads-catalog.json, embedded by gen_web_bundle.py and written to WEB_ROOT at
     * boot), so the page reads that directly and this end answers only what is TRUE RIGHT NOW and
     * cannot be known from a file. No JSON is parsed here on purpose: re-emitting the catalogue in
     * C would be a second implementation of a file the page can simply read, and every one of
     * those in this project has eventually disagreed with the first.
     *
     * Installing a seeded homebrew needs NO route of its own - /api/install already takes
     * install_key "local:<path>" and pkgfile_path_allowed() already accepts /data/... */
    if (!strcmp(path, "/api/payloads/upstream")) {
        /* What ONE project has released, and where this console can get the bytes.

           READ-ONLY AND INSTANT. Everything that talks to GitHub happens on the worker thread,
           because this server answers every request inline on a single accept loop: a route that
           waited for a TLS handshake would stop the console's whole API, and a panel following
           twenty sources would stop it for twenty seconds. So a project this console has not
           asked about yet answers {"ok":true,"pending":true} and is queued; the page polls and
           the drawer fills in as the answers arrive. That is also the kindest thing to do to an
           unauthenticated GitHub allowance of sixty calls an hour from one address.

           IT DOES NOT CHOOSE THE FILE. Every asset of the chosen release is reported and the
           page picks, because the page holds the catalogue and therefore the item's platform,
           kind, asset hint and extension - and because choosing is a rule the companion already
           implements (pick_asset), which a copy here would eventually disagree with. It already
           did: ftpsrv ships ftpsrv-ps4.elf and ftpsrv-ps5.elf in one release, and a first draft
           of this offered the PS4 build as the PS5's update. */
        char repo[160] = {0}, fb[8] = {0};
        qparam(rawpath, "repo", repo, sizeof(repo));
        int force = qparam(rawpath, "force", fb, sizeof(fb)) ? atoi(fb) : 0;
        /* owner/name, and nothing that could leave that shape. */
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
                          "%s{\"name\":\"%s\",\"url\":\"%s\",\"size\":%lld,"
                          "\"digest\":\"%s\"}",
                          i ? "," : "", an, au, snap.a[i].size, snap.a[i].digest);
        }
        if (n > 0 && n < 11900) snprintf(o + n, (size_t)(12000 - n), "]}");
        else                    snprintf(o, 12000,
                                         "{\"ok\":false,\"error\":\"that release lists more "
                                         "files than this console can report\"}");
        send_json(fd, o);
        free(o);
        return;
    }
    if (!strcmp(path, "/api/payloads/load")) {
        char want[256] = {0};
        /* rawpath, NOT req. qparam() takes the request PATH - every other caller in this file
           passes rawpath - and handing it the whole request text made it parse the query out of a
           line that still had " HTTP/1.1" on the end, so the name never matched anything and the
           console answered "this build does not carry that one" about a payload it was holding. */
        qparam(rawpath, "name", want, sizeof(want));
        /* BY STEM, NOT BY FILENAME. The page asks using the name the owner sees
           ("webkit-autoloader-installer_v0.5.1.elf") and this ELF carries it under the stable
           catalogue id ("webkit-autoloader-installer.elf"), because a versioned path in an .incbin
           breaks the build the first time an update lands. pm_stem() is what makes the two meet -
           the same function that matches a running process to the file it was built from. */
        char wstem[128];
        pm_stem(want, wstem, sizeof(wstem));
        /* A COPY THIS LANE DOWNLOADED WINS - AND ONLY ONE THIS LANE DOWNLOADED.
           Now that pm_stem strips a version suffix, a downloaded update shares its stem with the
           bundled copy, and asking the bundle first meant "I took the update" was followed by Run
           starting the shipped build - the same shape as the basename trap pldmgr has.
           Preferring ANY non-bundled file was tried and reversed, because "not one of ours" does
           not mean "newer": this folder holds second copies of three payloads that a PC left
           there, and OnionHEN.elf among them is 350 KB SMALLER than the onionhen.elf this build
           ships. The note is what was missing - only this lane writes one, so it is proof the
           owner asked for that file. Without a note the bundle still wins. */
        {
            char dn[300], dtag[160];
            if (pb_downloaded_find(wstem, dn, sizeof(dn), NULL, 0)) {
                dl_taken_version(PB_DIR, dn, dtag, sizeof(dtag));
                if (dtag[0]) {
                    char full[700];
                    if (pb_stage_for_pldmgr(dn, full, sizeof(full)) == 0) {
                        char q[800], o[520], edn2[320], edt[200];
                        snprintf(q, sizeof(q), "/loadpayload:%s", full);
                        int rc = pm_get(q);
                        json_escape(dn, edn2, sizeof(edn2));
                        json_escape(dtag, edt, sizeof(edt));
                        /* NAME WHAT WAS STARTED. A panel that cannot see which of two copies ran
                           is how "I took the update and it is still offered" reads as a bug in
                           the check rather than in the loading. */
                        snprintf(o, sizeof(o), rc == 0
                                 ? "{\"ok\":true,\"ran\":\"%s\",\"version\":\"%s\","
                                   "\"message\":\"Sent it to Payload Manager.\"}"
                                 : "{\"ok\":false,\"ran\":\"%s\",\"version\":\"%s\","
                                   "\"message\":\"Payload Manager did not take it.\"}",
                                 edn2, edt);
                        send_json(fd, o);
                        return;
                    }
                    /* It could not be staged - fall through to the bundled copy rather than
                       refuse. Something that runs is better than nothing. */
                }
            }
        }
        for (int i = 0; i < PAYLOAD_BUNDLE_COUNT; i++) {
            const pb_entry_t *e = &PAYLOAD_BUNDLE[i];
            char estem[128];
            pm_stem(e->filename, estem, sizeof(estem));
            if (strcmp(estem, wstem)) continue;
            char full[700];
            if (pb_stage_for_pldmgr(e->filename, full, sizeof(full)) != 0) {
                send_json(fd, "{\"ok\":false,\"message\":\"That one is not on this console yet.\"}");
                return;
            }
            char q[800];
            snprintf(q, sizeof(q), "/loadpayload:%s", full);
            int rc = pm_get(q);
            send_json(fd, rc == 0
                      ? "{\"ok\":true,\"message\":\"Sent it to Payload Manager.\"}"
                      : "{\"ok\":false,\"message\":\"Payload Manager did not take it.\"}");
            return;
        }
        /* NOT BUNDLED - BUT THE CONSOLE MAY HAVE DOWNLOADED IT. Before this, a payload fetched
           on the console could not be started at all: the loop above only ever matched what the
           ELF carries, so the answer was "this build does not carry that one" about a file
           sitting in PB_DIR. */
        {
            char dn[300];
            if (pb_downloaded_find(wstem, dn, sizeof(dn), NULL, 0)) {
                char full[700];
                if (pb_stage_for_pldmgr(dn, full, sizeof(full)) != 0) {
                    send_json(fd, "{\"ok\":false,\"message\":\"That one is on the console but "
                                  "could not be staged.\"}");
                    return;
                }
                char q[800];
                snprintf(q, sizeof(q), "/loadpayload:%s", full);
                int rc = pm_get(q);
                send_json(fd, rc == 0
                          ? "{\"ok\":true,\"message\":\"Sent it to Payload Manager.\"}"
                          : "{\"ok\":false,\"message\":\"Payload Manager did not take it.\"}");
                return;
            }
        }
        send_json(fd, "{\"ok\":false,\"message\":\"This console does not have that one.\"}");
        return;
    }
    if (!strcmp(path, "/api/tile/status") || !strcmp(path, "/api/tile/install")) {
        /* The dashboard tile: is it there, and put it back if not. */
        int want_install = !strcmp(path, "/api/tile/install");
        char fb[8] = {0};
        int force = qparam(rawpath, "force", fb, sizeof(fb)) ? atoi(fb) : 0;
        char detail[300] = {0};
        int rc = 0;
        if (want_install) rc = tile_install(force, detail, sizeof(detail));
        else              snprintf(detail, sizeof(detail), "%s",
                                   tile_is_registered() ? "registered" : "not registered");
        char ed[420], o[900];
        json_escape(detail, ed, sizeof(ed));
        snprintf(o, sizeof(o),
                 "{\"ok\":%s,\"title_id\":\"" TILE_TID "\",\"registered\":%s,\"rc\":%d,"
                 "\"url\":\"http://127.0.0.1:%d/\",\"detail\":\"%s\"}",
                 (want_install ? (rc == 0) : tile_is_registered()) ? "true" : "false",
                 tile_is_registered() ? "true" : "false", rc, PORT, ed);
        send_json(fd, o);
        return;
    }
    if (!strcmp(path, "/api/engine/install-dir")) {
        if (!diagnostics_allowed()) {
            send_status(fd, "403 Forbidden", "application/json",
                        "{\"ok\":false,\"error\":\"engine diagnostics are off - create /data/pkg-mutant-shop/allow-diagnostics to enable them\"}");
            return;
        }
        /* /api/engine/install-dir?tid=CUSAxxxxx[&uninstall=1]
           Registers /user/app/<tid> (which must already contain the laid-out title). */
        char tid[32] = {0}, ub[8] = {0};
        if (!qparam(rawpath, "tid", tid, sizeof(tid)) || !tid[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"missing tid\"}");
            return;
        }
        int un = qparam(rawpath, "uninstall", ub, sizeof(ub)) ? atoi(ub) : 0;
        int rcu = 0, rca = 0;
        int rc = install_title_dir(tid, un, &rcu, &rca);
        char out[520];
        snprintf(out, sizeof(out),
                 "{\"ok\":%s,\"rc\":\"0x%08X\",\"rc_uninstall\":\"0x%08X\",\"rc_installall\":\"0x%08X\","
                 "\"resolved\":{\"titledir\":%s,\"installall\":%s,\"uninstall\":%s},\"tid\":\"%s\"}",
                 rc == 0 ? "true" : "false", (unsigned)rc, (unsigned)rcu, (unsigned)rca,
                 g_ai_titledir ? "true" : "false", g_ai_installall ? "true" : "false",
                 g_ai_uninstall ? "true" : "false", tid);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/engine/install-local")) {
        if (!diagnostics_allowed()) {
            send_status(fd, "403 Forbidden", "application/json",
                        "{\"ok\":false,\"error\":\"engine diagnostics are off - create /data/pkg-mutant-shop/allow-diagnostics to enable them\"}");
            return;
        }
        /* Install a pkg ALREADY on the console (USB, internal, wherever) with
           sceAppInstUtilAppInstallPkg — the same call that installs our tile, so it is
           proven to work from this process. This is the offline lane: no PC, no network,
           no the third-party daemon. InstallByPackage is flaky even for the third-party daemon (their own runs fall
           back to exactly this), so local-file installs go straight here. */
        char p[1200] = {0};
        if (!qparam(rawpath, "path", p, sizeof(p)) || !p[0]) {
            send_json(fd, "{\"ok\":false,\"error\":\"missing path\"}");
            return;
        }
        char fixed[1300];
        rewrite_for_install(p, fixed, sizeof(fixed));
        char cid[64] = {0};
        int rc = install_pkg_local(fixed, cid, sizeof(cid));
        char eu[1400], out[1700];
        json_escape(fixed, eu, sizeof(eu));
        snprintf(out, sizeof(out),
                 "{\"ok\":%s,\"rc\":\"0x%08X\",\"via\":\"AppInstallPkg\",\"path\":\"%s\",\"content_id\":\"%s\"}",
                 rc == 0 ? "true" : "false", (unsigned)rc, eu, cid);
        send_json(fd, out);
        return;
    }
    if (!strcmp(path, "/api/health")) {
        /* Probe for real. These were two hardcoded booleans, so the console UI showed FTP as
           up and the install engine as down no matter what was actually running. */
        int ftp_up = ftp_live_port() != 0;
        /* THE REAL READINESS QUESTION. It used to be "is something listening on :12800" - a
           third-party daemon we no longer call. Our engine needs exactly one thing: Payload
           Manager, which spawns it. dpi_* is kept for compatibility with older UIs, but
           engine_ready is what actually predicts whether an install will work. */
        int engine_up = port_open(8084);
        int smp_up = port_open(SMP_API_PORT);
        int fport  = ftp_live_port();
        /* Roomy on purpose: snprintf truncates SILENTLY, and a health response cut mid-string
           is malformed JSON and a UI that cannot read health at all. The build stamp alone adds
           ~34 characters. Removing the four dead dpi_* fields left slack here - keep it. */
        /* The game on screen right now, so a page served BY this console floats it to the top
           of the library the way a page served by the PC always has. Two library calls behind a
           three-second memo - see running_title_cached. */
        char rt[24] = {0};
        running_title_cached(rt, sizeof(rt));
        /* See content_sig: two stats, so the companion can notice an install, an update, a delete
           or an add-on from the health poll it already makes, without pulling app.db. */
        char csig[96];      /* four %lld (20 each) and three dashes is 83 - sized from the format */
        content_sig(csig, sizeof(csig));
        char fw[16];
        fw_version_str(fw, sizeof(fw));
        char o[1040];       /* grew with "fw" - see the truncation guard below */
        snprintf(o, sizeof(o),
                 "{\"ok\":true,\"on_console\":true,\"server\":\"on-console\",\"connected\":true,"
                 "\"version\":\"" SHOP_VERSION "\",\"built\":\"" SHOP_BUILD "\","
                 "\"ps5_ip\":\"%s\","
                 "\"ftp_online\":%s,\"ftp_port\":%d,"
                 "\"shadowmount\":%s,\"shadowmount_port\":%d,"
                 "\"engine\":\"pms-spawn\",\"engine_ready\":%s,\"engine_port\":8084,"
                 "\"companion_port\":8710,\"lan_ip\":\"%s\",\"console_id\":\"%s\","
                 "\"running_title\":\"%s\",\"apps_sig\":\"%s\",\"fw\":\"%s\","
                 "\"thumb_route\":true,"
                 "\"conns\":%lld,\"uptime_s\":%lld}",
                 lan_ip_str(),
                 ftp_up ? "true" : "false", fport,
                 smp_up ? "true" : "false", SMP_API_PORT,
                 engine_up ? "true" : "false",
                 lan_ip_str(), console_id(), rt, csig, fw,
                 g_conns_served, g_started_ms ? (now_ms_local() - g_started_ms) / 1000 : 0);
        /* A HEALTH REPLY THAT GOT CUT IN HALF IS WORSE THAN A MISSING FIELD: every caller parses
           this as JSON, so a truncation turns "the console is fine" into "the console is broken".
           This has already happened once on this server, to /api/payloads, when its answer grew
           past a fixed buffer - so a reply that no longer fits says so in the log instead of going
           out malformed. */
        if ((size_t)snprintf(NULL, 0,
                 "{\"ok\":true,\"on_console\":true,\"server\":\"on-console\",\"connected\":true,"
                 "\"version\":\"" SHOP_VERSION "\",\"built\":\"" SHOP_BUILD "\","
                 "\"ps5_ip\":\"%s\",\"ftp_online\":%s,\"ftp_port\":%d,"
                 "\"shadowmount\":%s,\"shadowmount_port\":%d,"
                 "\"engine\":\"pms-spawn\",\"engine_ready\":%s,\"engine_port\":8084,"
                 "\"companion_port\":8710,\"lan_ip\":\"%s\",\"console_id\":\"%s\","
                 "\"running_title\":\"%s\",\"apps_sig\":\"%s\",\"fw\":\"%s\","
                 "\"thumb_route\":true,"
                 "\"conns\":%lld,\"uptime_s\":%lld}",
                 lan_ip_str(), ftp_up ? "true" : "false", fport,
                 smp_up ? "true" : "false", SMP_API_PORT, engine_up ? "true" : "false",
                 lan_ip_str(), console_id(), rt, csig, fw,
                 g_conns_served, g_started_ms ? (now_ms_local() - g_started_ms) / 1000 : 0) >= sizeof(o)) {
            ilog("health: reply did not fit in %zu bytes - a field was added without the buffer",
                 sizeof(o));
        }
        send_json(fd, o);
    } else if (!strcmp(path, "/api/library")) {
        char *j = build_library_json();
        if (j) { send_json(fd, j); free(j); } else send_json(fd, "{\"games\":[],\"console_reachable\":true}");
    } else if (!strcmp(path, "/api/installed")) {
        char *j = build_installed_json();
        if (j) { send_json(fd, j); free(j); } else send_json(fd, "{\"installed\":[]}");
    } else if (!strcmp(path, "/api/storage")) {
        char *j = build_library_json();   /* drives are embedded at the tail of the library json */
        if (j) {
            const char *dr = strstr(j, "\"drives\":");   /* ...],"drives":[{...}]}  -> ends the object */
            char out[2048];                             /* room for the PC tiles as well */
            if (dr) snprintf(out, sizeof(out), "{\"reachable\":true,%s", dr);
            send_json(fd, dr ? out : "{\"reachable\":true,\"drives\":[]}");
            free(j);
        } else send_json(fd, "{\"reachable\":true,\"drives\":[]}");
    } else if (!strncmp(path, "/api/icon/", 10)) {
        char tid[16]; snprintf(tid, sizeof(tid), "%.9s", path + 10);
        if (send_icon(fd, tid) != 0) send_status(fd, "404 Not Found", "text/plain", "no icon");
    } else if (!strncmp(path, "/icon/", 6)) {          /* UI convention: /icon/<TID>.png */
        char tid[16]; snprintf(tid, sizeof(tid), "%.9s", path + 6);
        if (send_icon(fd, tid) != 0) send_status(fd, "404 Not Found", "text/plain", "no icon");
    } else if (!strcmp(path, "/api/net/https")) {
        /* A PROBE, NOT A FEATURE. Behind the diagnostics flag because it reaches the internet
           from a console, and because its whole purpose is to answer one question before any
           user-facing lane is built on it: do these modules work from inside a payload? */
        if (!diagnostics_allowed()) {
            send_status(fd, "403 Forbidden", "application/json",
                        "{\"ok\":false,\"error\":\"create /data/pkg-mutant-shop/allow-diagnostics first\"}");
        } else {
            char url[700] = {0};
            if (!qparam(rawpath, "url", url, sizeof(url)) || !url[0])
                snprintf(url, sizeof(url), "https://api.github.com/rate_limit");
            char body[2048], errb[96] = {0};
            long long n = 0;
            char mb[8] = {0};
            int mode = qparam(rawpath, "mode", mb, sizeof(mb)) ? atoi(mb) : 0;
            body[0] = 0;
            int st = https_get_mode(url, body, sizeof(body), &n, errb, sizeof(errb), mode);
            char eb[200], bb[600];
            json_escape(errb, eb, sizeof(eb));
            json_escape(body, bb, sizeof(bb));
            char o[1100];
            snprintf(o, sizeof(o),
                     "{\"ok\":%s,\"status\":%d,\"bytes\":%lld,\"where\":\"%s\","
                     "\"ids\":\"%s\",\"head\":\"%.280s\"}",
                     (st >= 200 && st < 400) ? "true" : "false", st, n, eb, g_https_ids, bb);
            send_json(fd, o);
        }
    } else if (!strncmp(path, "/thumb/", 7)) {         /* the card-sized cover; see send_thumb */
        char tid[16]; snprintf(tid, sizeof(tid), "%.9s", path + 7);
        if (send_thumb(fd, tid) != 0) send_status(fd, "404 Not Found", "text/plain", "no icon");
    } else if (!strncmp(path, "/api/", 5)) {
        send_json(fd, "{}");           /* stub: unknown API endpoints stay quiet so the UI doesn't error */
    } else {
        serve_static(fd, path, req);
    }
}

/* ---- AppInstUtil installer (folded from tile-appinst): install ANY \x7fFIH pkg by path ---- */
int sceKernelLoadStartModule(const char *name, size_t argc, const void *argv,
                             uint32_t flags, void *opt, int *pRes);
int sceUserServiceInitialize(void *);
/* Whether anyone has signed in yet — the readiness gate the helper autostart waits on. */
int sceUserServiceGetLoginUserIdList(void *list);
#define APPINST_SPRX     "/system/common/lib/libSceAppInstUtil.sprx"
#define TILE_PKG_DISK    SHOP_DATA_DIR "/pms-tile.pkg"
/* The path we WRITE to and the path the INSTALLER must be given are different: our process
   sees /data, while SceShellCore (which actually performs the install) sees that same
   filesystem as /user/data. Handing it our own path is what makes AppInstallPkg answer
   0x80A40029 - it simply cannot see the file. */
#define TILE_PKG_INSTALL "/user/data/pkg-mutant-shop/pms-tile.pkg"
#define ENGINE_PKG_DISK    "/data/pldmgr/pms-installer.pkg"       /* install-engine (PKGM00002) reg pkg */
#define ENGINE_PKG_INSTALL "/user/data/pldmgr/pms-installer.pkg"

/* Full install (data + register) = the method the Debug Settings installer uses. BGFT is DISABLED on PS5;
   sceAppInstUtilInstallByPackage is its replacement. Struct layout per etaHEN's PS5 pkg writeup. */
typedef struct { char content_id[0x30]; int content_type; int content_platform; } SceAppInstallPkgInfo;
typedef struct { const char *uri; const char *ex_uri; const char *playgo_scenario_id;
                 const char *content_id; const char *content_name; const char *icon_url; } MetaInfo;
typedef struct { char languages[30][8]; char playgo_scenario_ids[64][3];
                 char content_ids[64][0x30]; unsigned char unknown[6480]; } PlayGoInfo;

#define AUTHID_JB         0x4801000000000013ULL   /* our jailbreak authid (jb.c) */

/* Linked directly (as the third-party daemon does). Runtime NID resolution stays as a fallback, but the
   linked symbols are authoritative: a stale NID silently calls the wrong function and looks
   exactly like a permission failure. */
extern int sceAppInstUtilInitialize(void);
extern int sceAppInstUtilInstallByPackage(MetaInfo *, SceAppInstallPkgInfo *, PlayGoInfo *);
extern int sceAppInstUtilAppInstallPkg(const char *, void *);

static int (*g_ai_init)(void) = 0;
static int (*g_ai_installpkg)(const char *, void *) = 0;                                /* AppInstallPkg — register only */
static int (*g_ai_installbypkg)(MetaInfo *, SceAppInstallPkgInfo *, PlayGoInfo *) = 0;  /* InstallByPackage — FULL install */

/* Load libSceAppInstUtil once and resolve the installers (needs escalation first). */
static void appinst_init_resolve(void);
static int g_ai_ready = 0;      /* module loaded + symbols resolved */
static int g_ai_inited = 0;     /* sceAppInstUtilInitialize called EXACTLY once, ever */

/* Resolve + initialise once. Calling sceAppInstUtilInitialize more than once silently
   resets the installer's IPMI state and makes later installs fail for no visible reason,
   so this is guarded and must be the ONLY place it is ever called. */
/* Modules the installer service depends on. It is IPMI-based, and a payload starts with a much
   smaller module set mapped than a full application does — the reference implementation drags
   these in via its wider library surface. Loading them explicitly is the cheap way to match. */
static const char *PRELOAD_SPRX[] = {
    "/system/common/lib/libSceIpmi.sprx",
    "/system/common/lib/libSceSysCore.sprx",
    "/system/common/lib/libSceSystemService.sprx",
    "/system/common/lib/libSceUserService.sprx",
    "/system/common/lib/libSceAppInstUtil.sprx",
};
static void preload_modules(void) {
    size_t n = 0;
    for (unsigned i = 0; i < sizeof(PRELOAD_SPRX) / sizeof(PRELOAD_SPRX[0]); i++) {
        int res = 0;
        int rc = sceKernelLoadStartModule(PRELOAD_SPRX[i], 0, NULL, 0, NULL, &res);
        const char *base = strrchr(PRELOAD_SPRX[i], '/');
        n += snprintf(g_preload_log + n, sizeof(g_preload_log) - n, "%s%s=%d",
                      n ? "," : "", base ? base + 1 : PRELOAD_SPRX[i], rc);
        if (n >= sizeof(g_preload_log) - 24) break;
    }
}

/* Under a lock, because "exactly once" was only ever true of a single thread: payload_bootstrap
   calls tile_install() on its own thread during the boot window while /api/tile/install,
   /api/engine/sym and the diagnostics routes reach here from the accept thread, and two callers
   passing the `!g_ai_inited` test together would have called Initialize twice - which the comment
   above says poisons the installer's IPMI state, silently. */
static pthread_mutex_t g_ai_lock = PTHREAD_MUTEX_INITIALIZER;
static int appinst_once(void) {
    pthread_mutex_lock(&g_ai_lock);
    if (!g_ai_ready) {
        sceUserServiceInitialize(0);
        preload_modules();
        /* prefer the linked symbols; fall back to NID resolution only if they're missing */
        g_ai_init         = sceAppInstUtilInitialize;
        g_ai_installbypkg = sceAppInstUtilInstallByPackage;
        g_ai_installpkg   = (int (*)(const char *, void *))sceAppInstUtilAppInstallPkg;
        if (!g_ai_init || !g_ai_installbypkg) appinst_init_resolve();
        g_ai_ready = 1;
    }
    if (!g_ai_inited && g_ai_init) { g_ai_init_rc = g_ai_init(); g_ai_inited = 1; }
    int rc = g_ai_init_rc;
    pthread_mutex_unlock(&g_ai_lock);
    return rc;
}

static void appinst_init_resolve(void) {
    sceUserServiceInitialize(0);
    int res = 0, mod = sceKernelLoadStartModule(APPINST_SPRX, 0, NULL, 0, NULL, &res);
    if (mod < 0) return;
    uint32_t h = 0;
    if (!kernel_dynlib_handle(-1, "libSceAppInstUtil.sprx", &h)) {
        g_ai_init         = (void *)kernel_dynlib_resolve(-1, h, "540lotO7oHE");
        g_ai_installpkg   = (void *)kernel_dynlib_resolve(-1, h, "bpLyMf0oVwQ");
        g_ai_installbypkg = (void *)kernel_dynlib_resolve(-1, h, "tDtjgaXYmuo");
    }
}



/* Auto-fire the install ENGINE app (PKGM00002): ask the shell to launch it so it runs InstallByPackage with
   its baked paid — no manual tile tap. NIDs resolved at runtime (like AppInstUtil) so linking can't break load. */
#define LNCUTIL_SPRX "/system/common/lib/libSceLncUtil.sprx"
int sceUserServiceGetForegroundUser(uint32_t *userId);   




/* FULL install of a local pkg — copies the game data + registers it. sceAppInstUtilInstallByPackage is
   gated behind ShellCore's authid (0x3800000000000010); set it, (re)init, install, restore our authid. */


/* Install a pkg (URL or /user/data path) straight from this process. No cred juggling:
   the server escalates itself at boot, which is the same configuration the third-party daemon's
   main process runs in — and that configuration is what Sony's PPR check accepts. */
int sceSystemServiceGetAppIdOfRunningBigApp(void);

/* Run the install on a dedicated thread and wait for it: the reference implementation always
   installs from a spawned worker, never from its request thread. */
typedef struct { const char *uri; char cid[64]; int rc; int ctype, cplat; } ibp_arg_t;
static void *ibp_thread(void *a) {
    ibp_arg_t *x = (ibp_arg_t *)a;
    PlayGoInfo *pg = calloc(1, sizeof(PlayGoInfo));
    if (!pg) { x->rc = -0x1002; return NULL; }
    MetaInfo meta; SceAppInstallPkgInfo pkg;
    memset(&meta, 0, sizeof(meta)); memset(&pkg, 0, sizeof(pkg));
    meta.uri = x->uri; meta.ex_uri = ""; meta.playgo_scenario_id = "";
    meta.content_id = ""; meta.content_name = "PKG MUTANT SHOP"; meta.icon_url = "";
    /* Always zero until now. Zero is proven to work for an ADD-ON; for an APP it has never been
       varied, and it is the last caller-supplied difference left. 0/0 = the historic behaviour. */
    pkg.content_type = x->ctype;
    pkg.content_platform = x->cplat;
    x->rc = g_ai_installbypkg(&meta, &pkg, pg);
    snprintf(x->cid, sizeof(x->cid), "%.48s", pkg.content_id);
    free(pg);
    return NULL;
}

/* ---- DIRECTORY install: register an already-laid-out title, no package auth ----------------
 * sceAppInstUtilAppInstallTitleDir(title_id, parent_dir, 0) registers /user/app/<TID> as an
 * installed app. There is no PKG involved, so the package-authentication check that returns
 * 0x80B2116F is never reached. AppInstallAll() is the batch equivalent (register everything
 * staged under /user/app). This is how fake-signed titles get installed on FW 12.x.
 * NID for AppInstallTitleDir is "Wudg3Xe3heE"; AppUnInstall clears a stale registration first. */
/* Linked directly — we already link libSceAppInstUtil, and hand-guessed NIDs resolved to
   nothing. These are the real exported names. */
/* DO NOT take the addresses of the SDK stubs for these: on 12.70 the stub thunk traps when
   called (it killed this process outright during testing). Resolve by NID only — a NULL result
   is a clean "unavailable" instead of a crash. AppInstallTitleDir's NID is the only one we have
   confirmed; the others must be recovered before this path can work. */
/* What the last resolve attempt saw, so a failure can be read off instead of guessed at. */
static uint32_t g_ai_dynh = 0;
static int      g_ai_handle_rc = -12345;

static void appinst_dir_resolve(void) {
    if (g_ai_titledir) return;
    uint32_t h = 0;
    g_ai_handle_rc = kernel_dynlib_handle(-1, "libSceAppInstUtil.sprx", &h);
    if (g_ai_handle_rc != 0) return;
    g_ai_dynh = h;
    /* By NID first, then BY NAME. The NID is a hash of the symbol and a firmware that exports it
       under a different one leaves us with a NULL and no explanation — which is exactly what
       happened here: install-dir answered rc=0xFFFFEFFD with "titledir":false and the dashboard
       tile could never be registered. kernel_dynlib_dlsym() asks for the symbol by its real name,
       so it does not depend on our NID being right for this firmware.
       The SDK's own stub is deliberately NOT used as a fallback: every symbol in
       libSceAppInstUtil.so shares one trap thunk (verified: all at 0x3148), and calling it
       killed this process outright during earlier testing. A NULL is a clean "unavailable". */
    if (!g_ai_titledir)
        g_ai_titledir   = (void *)kernel_dynlib_resolve(-1, h, "Wudg3Xe3heE");
    if (!g_ai_titledir)
        g_ai_titledir   = (void *)kernel_dynlib_dlsym(-1, h, "sceAppInstUtilAppInstallTitleDir");
    if (!g_ai_installall)
        g_ai_installall = (void *)kernel_dynlib_dlsym(-1, h, "sceAppInstUtilAppInstallAll");
    if (!g_ai_uninstall)
        g_ai_uninstall  = (void *)kernel_dynlib_dlsym(-1, h, "sceAppInstUtilAppUnInstall");
}

/* ===================== DASHBOARD TILE (PKGM00001) ===========================
   How the tiles that DO work on this console are actually built — read off the two homebrew
   tiles already installed (Elf Arsenal PSPS69691, Homebrew Launcher FAKE00000):

     * There is NO eboot and NO /system_ex/app/<TID> entry for either of them.
     * Each is a `deeplinkUri` tile: /user/appmeta/<TID>/param.json points at a LOCALHOST url
       (127.0.0.1:6969 and 127.0.0.1:8080), and tapping the tile opens that in the browser.
     * Registration is what creates /user/appmeta/<TID>; without it nothing appears.

   So ours points at 127.0.0.1:8710 — this very server. The tile then works with every PC on the
   network switched off, which the old build could not do: its eboot had a PC's LAN address baked
   in at compile time.

   Why it vanished: /user was wiped when the console was set up fresh, taking /user/app/PKGM00001
   and /user/appmeta/PKGM00001 with it, and boot-time registration had been removed on the
   assumption the tile was "already installed". It is installed here instead — but only when it is
   genuinely missing, which is what made the old boot-time version misbehave. */

static int tile_is_registered(void) {
    struct stat st;
    /* Two shapes count as installed, because there are two ways a tile gets here:
         /user/appmeta/<TID>/param.json  — what a PKG install produces
         /user/app/<TID>/sce_sys/param.json — what a deeplink registration produces, and where
                                              app.db's own metaDataPath points
       Checking only the first reported "not registered" for a tile that was live on the
       dashboard, which would also make the boot path re-stage it on every single load. */
    /* A real package install (sceAppInstUtilAppInstallPkg) is what creates /user/appmeta/<TID>.
       Nothing else counts: a hand-made /user/app/<TID>/sce_sys looked "installed" while the
       console had already garbage-collected the entry. */
    return stat(TILE_META_DIR "/param.json", &st) == 0;
}

static int copy_file_simple(const char *src, const char *dst) {
    FILE *i = fopen(src, "rb");
    if (!i) return -1;
    FILE *o = fopen(dst, "wb");
    if (!o) { fclose(i); return -2; }
    char *b = (char *)malloc(65536);           /* heap: this can run on a worker thread */
    if (!b) { fclose(i); fclose(o); return -3; }
    size_t n;
    while ((n = fread(b, 1, 65536, i)) > 0) {
        if (fwrite(b, 1, n, o) != n) { free(b); fclose(i); fclose(o); return -4; }
    }
    free(b); fclose(i); fclose(o);
    return 0;
}

/* Install the dashboard tile by handing our embedded package to the system installer.

   This is the method that actually works on FW 12.70, and the one this project used before:
   write the fake-signed tile PKG out of the ELF and call sceAppInstUtilAppInstallPkg, which
   queues the job on SceShellCore's own installer — the same path a store download takes. The
   installer creates /user/appmeta/PKGM00001 and the database rows itself, so the tile shows up
   live with no reboot, and running it again just updates the entry in place.

   Returns 0 on success, negative otherwise; `detail` says what happened. */
static int tile_install(int force, char *detail, size_t dsz) {
    if (!force && tile_is_registered()) {
        snprintf(detail, dsz, "already registered");
        return 0;
    }
    size_t pkg_len = (size_t)(tb_tile_pkg_end - tb_tile_pkg);
    if (pkg_len < 4096 || tb_tile_pkg[0] != 0x7F || tb_tile_pkg[1] != 'F') {
        snprintf(detail, dsz, "embedded tile package looks wrong (%zu bytes)", pkg_len);
        return -1;
    }
    mkdir(SHOP_DATA_DIR, 0777);
    FILE *f = fopen(TILE_PKG_DISK, "wb");
    if (!f) { snprintf(detail, dsz, "cannot write %s", TILE_PKG_DISK); return -2; }
    size_t wrote = fwrite(tb_tile_pkg, 1, pkg_len, f);
    fclose(f);
    if (wrote != pkg_len) {
        snprintf(detail, dsz, "short write %zu/%zu", wrote, pkg_len);
        return -3;
    }
    /* The installer runs a PPR auth check, which is why install_full() borrows SceShellCore's
       credentials for InstallByPackage. AppInstallPkg is gated the same way — without this it
       answers 0x80A40029 no matter how correct the package is. Ours are always restored. */
    char cid[80] = {0};
    pid_t me = getpid();
    uint64_t my_authid = kernel_get_ucred_authid(me);
    uint8_t my_caps[16], my_attrs[32];
    kernel_get_ucred_caps(me, my_caps);
    kernel_get_ucred_attrs(me, my_attrs);
    int cred_pid = find_pid_by_authid(AUTHID_SHELLCORE, 900);
    if (cred_pid > 0) {
        uint8_t c[16], a[32];
        uint64_t aid = kernel_get_ucred_authid(cred_pid);
        kernel_get_ucred_caps(cred_pid, c);
        kernel_get_ucred_attrs(cred_pid, a);
        if (aid) {
            kernel_set_ucred_authid(me, aid);
            kernel_set_ucred_caps(me, c);
            kernel_set_ucred_attrs(me, a);
        }
    }
    int rc = install_pkg_local(TILE_PKG_INSTALL, cid, sizeof(cid));
    kernel_set_ucred_authid(me, my_authid);
    kernel_set_ucred_caps(me, my_caps);
    kernel_set_ucred_attrs(me, my_attrs);
    /* The installer runs asynchronously: give it a moment, then look for the metadata it
       creates rather than trusting the return code alone. */
    int reg = 0;
    for (int i = 0; i < 20 && !reg; i++) { sleep(1); reg = tile_is_registered(); }
    snprintf(detail, dsz, "pkg=%zuB rc=0x%08X cred_pid=%d cid=%s registered=%s",
             pkg_len, (unsigned)rc, cred_pid, cid[0] ? cid : "-", reg ? "yes" : "no");
    return reg ? 0 : (rc ? rc : -4);
}

/* Register /user/app/<tid> as an installed title. 0 on success. */
static int install_title_dir(const char *tid, int do_uninstall_first,
                             int *rc_uninstall, int *rc_all) {
    appinst_once();
    appinst_dir_resolve();
    if (rc_uninstall) *rc_uninstall = 0x7FFFFFFF;
    if (rc_all) *rc_all = 0x7FFFFFFF;
    if (do_uninstall_first && g_ai_uninstall) {
        int u = g_ai_uninstall(tid);
        if (rc_uninstall) *rc_uninstall = u;
    }
    if (g_ai_titledir) {
        int rc = g_ai_titledir(tid, "/user/app/", 0);
        if (rc == 0) return 0;
        if (g_ai_installall) { int a = g_ai_installall(0); if (rc_all) *rc_all = a; if (a == 0) return 0; }
        return rc;
    }
    if (g_ai_installall) { int a = g_ai_installall(0); if (rc_all) *rc_all = a; return a; }
    return -0x1003;
}

static int install_by_package_inproc_raw(const char *uri, char *cid_out, size_t cid_sz,
                                         int announce_bigapp, int ctype, int cplat) {
    appinst_once();
    if (!g_ai_installbypkg) return -0x1001;
    /* The comment that used to sit here said this call was "something the working reference
       implementation does". That is FALSE, established by reading etaHEN's own GPLv3 source:
       DirectPKGInstaller.cpp - the file that actually performs its installs - never calls
       sceSystemServiceGetAppIdOfRunningBigApp. etaHEN imports the symbol and uses it in its
       daemon/jailbreak code (commands.cpp, msg.cpp), never on the install path.

       It is now a parameter, because it is the leading suspect for why we can create BGFT
       subtype=7 (add-on) install tasks but not subtype=6 (game) ones. Announcing a running big
       app immediately before an APP install plausibly steers ShellCore down the
       applyPatchPrimarySlot route - which is exactly the call that errors in our failing trace,
       and which an add-on install never touches. That asymmetry is the whole puzzle.

       DEFAULT IS UNCHANGED (announce_bigapp = 1) so every existing caller behaves exactly as
       before. Only the A/B probe passes 0. */
    if (announce_bigapp) sceSystemServiceGetAppIdOfRunningBigApp();
    {
        ibp_arg_t a; memset(&a, 0, sizeof(a)); a.uri = uri; a.rc = -1;
        a.ctype = ctype; a.cplat = cplat;
        pthread_t th;
        pthread_attr_t ia2;
        pthread_attr_init(&ia2);
        pthread_attr_setstacksize(&ia2, 256 * 1024);   /* calls into Sony's installer */
        if (pthread_create(&th, &ia2, ibp_thread, &a) == 0) {
            pthread_join(th, NULL);
            if (cid_out && cid_sz) snprintf(cid_out, cid_sz, "%s", a.cid);
            return a.rc;
        }
    }
    PlayGoInfo *pg = calloc(1, sizeof(PlayGoInfo));   /* ~10KB — keep it off the stack */
    if (!pg) return -0x1002;
    MetaInfo meta;
    SceAppInstallPkgInfo pkg;
    memset(&meta, 0, sizeof(meta));
    memset(&pkg, 0, sizeof(pkg));
    meta.uri = uri; meta.ex_uri = ""; meta.playgo_scenario_id = "";
    meta.content_id = ""; meta.content_name = "PKG MUTANT SHOP"; meta.icon_url = "";
    /* SceAppInstallPkgInfo has always been passed as all zeros, on the assumption that content_id
       is purely an OUT field and the other two do not matter. Zero demonstrably works for an
       ADD-ON. Nobody has ever tried a non-zero content_type/content_platform for an APP, and that
       is the last caller-supplied difference left untested - everything about the caller's
       identity and context has now been eliminated by experiment. */
    pkg.content_type = ctype;
    pkg.content_platform = cplat;
    int rc = g_ai_installbypkg(&meta, &pkg, pg);
    if (cid_out && cid_sz) snprintf(cid_out, cid_sz, "%.48s", pkg.content_id);
    free(pg);
    return rc;
}

/* Call InstallByPackage as a DIFFERENT authid for the duration of one attempt.

   WHY THIS EXISTS - the measurement, taken on this console on 2026-08-24 with the same package
   (Riptide GP2, EP0786-CUSA02365_00) nine minutes apart:

     ours (0x80B2116F)                        etaHEN (success)
     -----------------------------------      -----------------------------------
     GetRawContentInfo            0x0         GetRawContentInfo            0x0
     [AppInstaller] err @18451                [AppInstaller] err @18451
     applyPatchPrimarySlot   0x80a30004       applyPatchPrimarySlot   0x80a30004
     AppPrepareOverwriteByPackage(cid)        AppPrepareOverwriteByPackage(cid)
     [AppPromoter] err @9377                  [AppPromoter] err @9377
     GetPrimaryAppSlot         0x80a3000e     GetPrimaryAppSlot         0x80a3000e
     AppPrepareOverwriteByPackage  0x0        AppPrepareOverwriteByPackage  0x0
     [DbgInstall] begin                       [DbgInstall] begin
     STORAGE enqueue start m.2 0xfb7a10000    STORAGE enqueue start m.2 0xfb7a10000
     DownloadMultiStreamStat      0x0         DownloadMultiStreamStat      0x0
     .                                        [DbgInstall] Staring Pre-allocation transfer
     .                                        BGFT Task [type=1, subtype=6] : created
     STORAGE enqueue end   m.2 0xfb7a10000    STORAGE enqueue end   m.2 0xfb12a0000
     [DbgInstall] end        0x80b2116f       [DbgInstall] end             0x0

   Byte-identical until pre-allocation; we reserve ZERO bytes and are refused with INVALID_SLOT.
   Everything ShellCore DERIVES about the caller is the same for both (getAppId resolves to
   0xffffffff either way). What is left is what the IPC CARRIES - the caller's authid.
   jb_escalate_pid() gives us the standard homebrew 0x4801000000000013.

   The swap is on OUR OWN process and is put back immediately; jb_escalate_pid already writes this
   exact field at boot with the same kernel primitive, so this is not a new class of operation. It
   is restored even when the install call fails, because leaving the process on a borrowed authid
   would change how every later request behaves. */
/* MEASURED 2026-08-25, and this is why the block below exists.

   Same package, same URI string, same console, the same second:
       ours    sceAppInstUtilInstallByPackage -> 0x80B2116F
       etaHEN  the identical call             -> SUCCESS
   Identical bytes in, different result out. The ARGUMENTS are eliminated; the difference is a
   property of the calling PROCESS.

   Diffing our process against etaHEN's util daemon (/api/engine/procdiff) leaves exactly four:

       field     ours                    etaHEN util (succeeds)
       authid    0x4801000000000013      0x4800000000000006     <- already A/B'd, no effect
       r/sv uid  0                       1
       gid       0                       1
       jaildir   0xffffb84801d82ee0      0x0                    <- WE ARE JAILED, THEY ARE NOT
       caps      ff x16                  00000000701c004000ff0000000000c0
       attrs     (identical)             (identical)            <- eliminated

   jaildir is the striking one: jb_escalate_pid() sets rootdir AND jaildir to the root vnode,
   while etaHEN sets only rootdir and leaves jaildir NULL. A non-NULL jaildir means the process
   sits inside a jail. And our caps are a blunt memset(0xff) - "every bit set" is not the same as
   "maximum privilege", and a bit that should be zero is a plausible thing for ShellCore to check
   when it builds an APP actor and not when it builds an add-on actor.

   Everything here is applied to OUR OWN process for the duration of one call and restored on
   every exit path. Defaults leave behaviour exactly as before. */
static int install_by_package_inproc_ex2(const char *uri, char *cid_out, size_t cid_sz,
                                         int announce_bigapp, int ctype, int cplat,
                                         const cred_profile_t *cp) {
    pid_t me = getpid();
    unsigned long long saved_auth = 0;
    intptr_t saved_jail = 0;
    unsigned char saved_caps[16];
    int did_auth = 0, did_jail = 0, did_caps = 0, did_uids = 0;

    if (cp && cp->authid) {
        saved_auth = kernel_get_ucred_authid(me);
        if (saved_auth && kernel_set_ucred_authid(me, cp->authid) == 0) did_auth = 1;
    }
    if (cp && cp->set_jaildir) {
        saved_jail = kernel_get_proc_jaildir(me);
        if (kernel_set_proc_jaildir(me, cp->jaildir_null ? 0 : saved_jail) == 0) did_jail = 1;
    }
    if (cp && cp->set_caps) {
        if (kernel_get_ucred_caps(me, saved_caps) == 0 &&
            kernel_set_ucred_caps(me, cp->caps) == 0) did_caps = 1;
    }
    if (cp && cp->set_uids) {
        kernel_set_ucred_ruid(me, cp->uid_val);
        kernel_set_ucred_svuid(me, cp->uid_val);
        kernel_set_ucred_rgid(me, cp->uid_val);
        kernel_set_ucred_svgid(me, cp->uid_val);
        did_uids = 1;
    }

    int rc = install_by_package_inproc_raw(uri, cid_out, cid_sz, announce_bigapp, ctype, cplat);

    /* Restore, always. Leaving the process on a borrowed credential changes how every later
       request behaves - and a wrong caps mask would quietly break our own file access. */
    if (did_uids) {
        kernel_set_ucred_ruid(me, 0); kernel_set_ucred_svuid(me, 0);
        kernel_set_ucred_rgid(me, 0); kernel_set_ucred_svgid(me, 0);
    }
    if (did_caps) kernel_set_ucred_caps(me, saved_caps);
    if (did_jail) kernel_set_proc_jaildir(me, saved_jail);
    if (did_auth) kernel_set_ucred_authid(me, saved_auth);
    return rc;
}

static int install_by_package_inproc_ex(const char *uri, char *cid_out, size_t cid_sz,
                                        int announce_bigapp, unsigned long long authid,
                                        int ctype, int cplat) {
    cred_profile_t cp;
    memset(&cp, 0, sizeof(cp));
    cp.authid = authid;
    return install_by_package_inproc_ex2(uri, cid_out, cid_sz, announce_bigapp, ctype, cplat, &cp);
}

/* The original signature, behaviour bit-for-bit identical to before this split. */
static int install_by_package_inproc(const char *uri, char *cid_out, size_t cid_sz) {
    return install_by_package_inproc_ex(uri, cid_out, cid_sz, 1, 0, 0, 0);
}

/* Install a pkg that is already on the console. sceAppInstUtilAppInstallPkg is what
   registers our tile today, so it is known-good from this process; the third-party daemon uses the
   same call as its final fallback for real games. Path must be /user/data/... */
static int install_pkg_local(const char *path, char *cid_out, size_t cid_sz) {
    appinst_once();
    if (!g_ai_installpkg) return -0x1001;
    SceAppInstallPkgInfo info;
    memset(&info, 0, sizeof(info));
    int rc = g_ai_installpkg(path, &info);
    if (cid_out && cid_sz) snprintf(cid_out, cid_sz, "%.48s", info.content_id);
    return rc;
}

/* Find a process by its authid by walking pids. We need SceShellCore's credentials to
   satisfy the installer's PPR check, and its pid changes every boot, so it has to be
   discovered rather than hard-coded. ShellCore = AUTHID_SHELLCORE (0x48... on 12.70; the
   PS4-era 0x38... value this comment used to quote does not exist on this firmware). */
static int find_pid_by_authid(uint64_t want, int maxpid) {
    for (int pid = 1; pid < maxpid; pid++) {
        if (kernel_get_ucred_authid(pid) == want) return pid;
    }
    return 0;
}

/* FULL data install. sceAppInstUtilAppInstallPkg only REGISTERS metadata (it returns in
   under a second for a 7 GB package and leaves /user/app/<TID> missing, so the tile appears
   but the game cannot start). Real installs go through InstallByPackage, which is gated by
   Sony's PPR auth — so we borrow SceShellCore's exact credentials for the call and put ours
   back immediately afterwards. */
static int install_full(const char *uri, int cred_pid, char *cid_out, size_t cid_sz, int *used_pid) {
    appinst_once();
    if (!g_ai_installbypkg) return -0x1001;

    if (cred_pid <= 0) cred_pid = find_pid_by_authid(AUTHID_SHELLCORE, 900);
    if (used_pid) *used_pid = cred_pid;

    pid_t me = getpid();
    uint64_t my_authid = kernel_get_ucred_authid(me);
    uint8_t my_caps[16], my_attrs[32];
    kernel_get_ucred_caps(me, my_caps);
    kernel_get_ucred_attrs(me, my_attrs);

    if (cred_pid > 0) {
        uint8_t c[16], a[32];
        uint64_t aid = kernel_get_ucred_authid(cred_pid);
        kernel_get_ucred_caps(cred_pid, c);
        kernel_get_ucred_attrs(cred_pid, a);
        if (aid) {
            kernel_set_ucred_authid(me, aid);
            kernel_set_ucred_caps(me, c);
            kernel_set_ucred_attrs(me, a);
        }
    }

    int rc = -0x1002;
    PlayGoInfo *pg = calloc(1, sizeof(PlayGoInfo));
    if (pg) {
        MetaInfo meta;
        SceAppInstallPkgInfo pkg;
        memset(&meta, 0, sizeof(meta));
        memset(&pkg, 0, sizeof(pkg));
        meta.uri = uri; meta.ex_uri = ""; meta.playgo_scenario_id = "";
        meta.content_id = ""; meta.content_name = "PKG MUTANT SHOP"; meta.icon_url = "";
        rc = g_ai_installbypkg(&meta, &pkg, pg);
        if (cid_out && cid_sz) snprintf(cid_out, cid_sz, "%.48s", pkg.content_id);
        free(pg);
    }

    /* always restore our own credentials, even on failure */
    kernel_set_ucred_authid(me, my_authid);
    kernel_set_ucred_caps(me, my_caps);
    kernel_set_ucred_attrs(me, my_attrs);
    return rc;
}

/* ---------------- bundled helper payloads ------------------------------------------------
 * ShadowMount (PS5 backups) and the install host ship INSIDE this ELF, so loading one file
 * gives the user the whole service with nothing else to install.
 *
 * We write them to disk and ask Payload Manager to launch them. Two rules keep this safe:
 *   - never relaunch something already running (that is how the install host gets wedged), and
 *   - never block boot: this runs on its own thread, after the HTTP server is already serving.
 * ---------------------------------------------------------------------------------------- */
/* PB_DIR and HB_DIR are declared with the other shop paths near the top - the routes that read
   them are several thousand lines above this point. */
/* CR/LF as explicit codes - avoids any escaping issues in the request line */
static const char PM_HDR_TAIL[] = { 13,10, 'H','o','s','t',':',' ','1','2','7','.','0','.','0','.','1', 13,10,
  'C','o','n','n','e','c','t','i','o','n',':',' ','c','l','o','s','e', 13,10, 13,10, 0 };

/* one GET to Payload Manager; 0 on success.

   Success means Payload Manager ACCEPTED the load: an HTTP 2xx, or - should it ever answer
   without a status line - the bare "OK" its /loadpayload is documented to return. Any bytes at
   all used to count, so an error body from pldmgr logged "installer spawned rc=0" and the lane
   then waited 60-90 s for a verdict that could never come before blaming a busy console. The
   receive timeout is 10 s, not 30: this runs on the accept thread for install-spawn, and a hung
   Payload Manager froze every API on this console for half a minute per request. */
/* Is a payload running, by the stem of its name?

   A PORT IS EVIDENCE, NOT THE ONLY EVIDENCE - measured on this console 2026-09-30, Payload Manager
   lists shadowmountplus.elf at a live pid while :10101 is closed, and kstuff and nanodns bind
   nothing a TCP probe can reach at all. So the process list is asked first and the port is the
   second opinion.

   STEMS, NOT FILENAMES. pldmgr reports the name a payload was BUILT as, which is not always the
   name of the file we ship: "ftpsrv-ps5.elf" runs as "ftpsrv.elf" and "pldmgr_v0.5.2.elf" as
   "pldmgr.elf". Comparing raw names called two live payloads stopped. */
static void pm_stem(const char *name, char *out, size_t outsz) {
    size_t n = 0;
    for (const char *p = name; *p && n + 1 < outsz; p++)
        out[n++] = (char)((*p >= 'A' && *p <= 'Z') ? *p - 'A' + 'a' : *p);
    out[n] = 0;
    if (n > 4 && !strcmp(out + n - 4, ".elf")) { n -= 4; out[n] = 0; }
    /* trailing -ps4 / -ps5 / _ps4 / _ps5 */
    if (n > 4 && (out[n - 4] == '-' || out[n - 4] == '_') && out[n - 3] == 'p' && out[n - 2] == 's'
        && (out[n - 1] == '4' || out[n - 1] == '5')) { n -= 4; out[n] = 0; }
    /* trailing _v1.2.3 / -v1.2 / _1.2 */
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
            if (j < n && out[j] >= '0' && out[j] <= '9') { out[i] = 0; }
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

/* Put a payload where Payload Manager will actually find it, and give back that path.

   THE BASENAME TRAP, WHICH THIS PROJECT HAS ALREADY PAID FOR ONCE. /loadpayload resolves by
   BASENAME against pldmgr's own registered directory, not by the path it is handed - so asking it
   to load /data/pkg-mutant-shop/payloads/ftpsrv.elf gets "Payload Manager did not take it" when
   nothing of that name is registered there. companion/deploy.py already writes our own ELF to
   /data/pldmgr/payloads/<NAME>/<NAME>.elf for exactly this reason; this does the same for the
   payloads we carry, so a console can start one with no PC involved at all.

   Copied only when the destination differs, so pressing Run twice costs one stat. */
/* Is this exact file one of the payloads this ELF carries? The same question
   pms_name_is_bundled answers for the download lane's cleanup, under the name this side uses. */
static int pb_name_is_bundled(const char *name) {
    return pms_name_is_bundled(name);
}

/* A payload in PB_DIR that this build does NOT carry - i.e. one the console downloaded.
   `want_stem` of NULL means "the first one found"; otherwise only that stem matches.
   Fills `name` with the file name and returns 1 when something matched.

   BY FILENAME, NOT BY STEM. This used to skip any file whose STEM was a bundled one - a way of
   keeping a leftover from an older build out of the comparison. But now that pm_stem strips a
   version suffix (as it always said it did), a downloaded update shares its stem with the
   bundled copy BY CONSTRUCTION, so that rule hid every one of them: measured on the PS5, which
   downloaded webkit-autoloader-installer_v0.6.1.elf and then reported only the build's own
   webkit-autoloader-installer.elf. The exact filename is the honest test, and the download lane
   removes same-stem files as each new one lands, so a folder holds at most one download per
   payload. */
static int pb_downloaded_find(const char *want_stem, char *name, size_t namesz,
                              long long *size, int skip) {
    DIR *d = opendir(PB_DIR);
    if (!d) return 0;
    struct dirent *de;
    int seen = 0, got = 0;
    while ((de = readdir(d)) != NULL) {
        if (de->d_name[0] == '.') continue;
        char full[700];
        snprintf(full, sizeof(full), "%s/%s", PB_DIR, de->d_name);
        struct stat st;
        if (stat(full, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size <= 0) continue;
        if (pb_name_is_bundled(de->d_name)) continue;
        char stem[128];
        pm_stem(de->d_name, stem, sizeof(stem));
        if (!stem[0]) continue;
        if (want_stem && strcmp(stem, want_stem)) continue;
        if (seen++ < skip) continue;
        snprintf(name, namesz, "%s", de->d_name);
        if (size) *size = (long long)st.st_size;
        got = 1;
        break;
    }
    closedir(d);
    return got;
}

static int pb_stage_for_pldmgr(const char *name, char *out, size_t outsz) {
    char stem[128];
    pm_stem(name, stem, sizeof(stem));
    if (!stem[0]) return -1;
    char src[600], dir[600];
    snprintf(src, sizeof(src), "%s/%s", PB_DIR, name);
    struct stat ss;
    if (stat(src, &ss) != 0 || ss.st_size <= 0) return -1;
    mkdir("/data/pldmgr", 0777);
    mkdir("/data/pldmgr/payloads", 0777);
    snprintf(dir, sizeof(dir), "/data/pldmgr/payloads/%s", stem);
    mkdir(dir, 0777);
    snprintf(out, outsz, "%s/%s.elf", dir, stem);

    struct stat ds;
    if (stat(out, &ds) == 0 && ds.st_size == ss.st_size) return 0;   /* already the same bytes */

    int in = open(src, O_RDONLY);
    if (in < 0) return -1;
    char part[700];
    snprintf(part, sizeof(part), "%s.part", out);
    int of = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (of < 0) { close(in); return -1; }
    char buf[65536];
    ssize_t r;
    int bad = 0;
    /* write() in a loop rather than write_all(): this file's write_all returns void, so a short or
       failed write would be invisible - and a truncated payload that Payload Manager then runs is
       the worst way to find that out. */
    while ((r = read(in, buf, sizeof(buf))) > 0) {
        size_t left = (size_t)r;
        const char *p = buf;
        while (left) {
            ssize_t w = write(of, p, left);
            if (w <= 0) { bad = 1; break; }
            p += w; left -= (size_t)w;
        }
        if (bad) break;
    }
    close(in);
    close(of);
    if (bad || r < 0) { unlink(part); return -1; }
    /* rename onto an existing file does not work on these consoles */
    if (rename(part, out) != 0) { unlink(out); if (rename(part, out) != 0) { unlink(part); return -1; } }
    return 0;
}

#define PM_LIST_MAX 16384
/* THE PROCESS LIST, FETCHED ONCE AND SHARED FOR A MOMENT.
 *
 * pm_running() used to open its own connection to the payload manager and read the WHOLE process
 * list every time it was asked - and /api/payloads asks it once per entry, eight times, on the
 * single accept loop. Eight identical round trips for one panel, and with a six-second read timeout
 * on each that is up to 48 seconds of a console that answers nothing else while it waits. The list
 * is one answer to one question; fetch it once.
 *
 * The TTL only has to outlive a single pass over the panel, so it is short enough that nothing can
 * read as running after it stopped. The read timeout comes down with it: this is loopback, where
 * the payload manager replies in milliseconds, and six seconds was only ever generous-by-default. */
#define PM_LIST_TTL_MS 1500
static int pm_list_get(char *out, size_t outsz) {
    static char cache[PM_LIST_MAX];
    static size_t clen = 0;
    static long long cms = 0;
    static pthread_mutex_t lk = PTHREAD_MUTEX_INITIALIZER;

    pthread_mutex_lock(&lk);
    if (clen && (now_ms_local() - cms) < PM_LIST_TTL_MS && clen + 1 <= outsz) {
        memcpy(out, cache, clen + 1);
        size_t l = clen;
        pthread_mutex_unlock(&lk);
        return (int)l;
    }
    pthread_mutex_unlock(&lk);

    int s = connect_local(PLDMGR_PORT, 2500, 2500);
    if (s < 0) return -1;
    char req[256];
    int n = snprintf(req, sizeof(req), "GET /processes_list HTTP/1.0%s", PM_HDR_TAIL);
    write_all(s, req, (size_t)n);
    /* 16 KB: this console answers with 89 processes in about 3.5 KB. A list that outgrows the
       buffer simply stops being scanned, which under-reports rather than inventing a green light. */
    char *b = (char *)malloc(PM_LIST_MAX);
    if (!b) { close(s); return -1; }
    size_t got = 0;
    for (;;) {
        ssize_t r = read(s, b + got, PM_LIST_MAX - 1 - got);
        if (r <= 0) break;
        got += (size_t)r;
        if (got >= PM_LIST_MAX - 1) break;
    }
    close(s);
    b[got] = 0;
    for (size_t i = 0; i < got; i++)
        if (b[i] >= 'A' && b[i] <= 'Z') b[i] = (char)(b[i] - 'A' + 'a');

    pthread_mutex_lock(&lk);
    memcpy(cache, b, got + 1);
    clen = got;
    cms = now_ms_local();
    pthread_mutex_unlock(&lk);

    if (got + 1 > outsz) { free(b); return -1; }
    memcpy(out, b, got + 1);
    free(b);
    return (int)got;
}

static int pm_running(const char *filename) {
    char want[128];
    pm_stem(filename, want, sizeof(want));
    if (!want[0]) return 0;
    char *b = (char *)malloc(PM_LIST_MAX);
    if (!b) return 0;
    int gl = pm_list_get(b, PM_LIST_MAX);
    if (gl < 0) { free(b); return 0; }
    size_t got = (size_t)gl;
    int hit = 0;
    const char *p = b;
    while ((p = strstr(p, "\"name\"")) != NULL) {
        const char *q = strchr(p + 6, '"');
        if (!q) break;
        q++;
        const char *e = strchr(q, '"');
        if (!e) break;
        char nm[128], st[128];
        size_t L = (size_t)(e - q);
        if (L >= sizeof(nm)) L = sizeof(nm) - 1;
        memcpy(nm, q, L); nm[L] = 0;
        pm_stem(nm, st, sizeof(st));
        if (!strcmp(st, want)) { hit = 1; break; }
        p = e;
    }
    free(b);
    return hit;
}


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
static const char *APP_DATA_ROOTS[] = { "/user/app", "/mnt/ext0/user/app",
                                        "/mnt/ext1/user/app", "/mnt/ext2/user/app", NULL };

/* Does this title really have its data? Two ways, and only two.

   app.pkg WITH BYTES is the proof for anything the console INSTALLED - it is what the install lane
   itself waits for before it will call a job done.

   mount.lnk is the proof for a title that is MOUNTED rather than installed: a folder app sent to
   the drive ShadowMountPlus watches has no app.pkg and never will, because nothing was installed -
   the container is mounted in place. This file is what ShadowMount leaves for exactly that, and it
   is what this same server already looks for when it is asked whether a container is mounted. A
   title the owner can start from the home screen that this function called "not installed" is a
   wrong answer, whichever of the two routes put it there. */
static int title_has_data(const char *tid) {
    for (int i = 0; APP_DATA_ROOTS[i]; i++) {
        char p[700];
        struct stat st;
        snprintf(p, sizeof(p), "%s/%s/app.pkg", APP_DATA_ROOTS[i], tid);
        if (stat(p, &st) == 0 && S_ISREG(st.st_mode) && st.st_size > 0) return 1;
        snprintf(p, sizeof(p), "%s/%s/mount.lnk", APP_DATA_ROOTS[i], tid);
        if (stat(p, &st) == 0) return 1;
    }
    return 0;
}

static int app_ids_json(char *out, size_t outsz) {
    int n = 0;
    out[0] = 0;
    for (int r = 0; APP_DATA_ROOTS[r] && n < (int)outsz - 40; r++) {
        DIR *d = opendir(APP_DATA_ROOTS[r]);
        if (!d) continue;
        struct dirent *de;
        while ((de = readdir(d)) && n < (int)outsz - 40) {
            if (de->d_name[0] == '.') continue;
            size_t L = strlen(de->d_name);
            if (L < 6 || L > 12) continue;
            if (!title_has_data(de->d_name)) continue;
            n += snprintf(out + n, outsz - (size_t)n, "%s\"%s\"", n ? "," : "", de->d_name);
        }
        closedir(d);
    }
    return n;
}

static const char *HB_ROOTS[] = {
    HB_DIR,
    "/mnt/usb0/homebrews", "/mnt/usb1/homebrews", "/mnt/usb2/homebrews", "/mnt/usb3/homebrews",
    "/mnt/ext0/homebrews", "/mnt/ext1/homebrews", "/mnt/ext2/homebrews",
    "/mnt/usb0", "/mnt/usb1", "/mnt/ext0", "/mnt/ext1",
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
            /* The release this lane took, when it was this lane that fetched it - the panel
               has no other way to know what version a package on disk is. */
            char hv[160], ehv[200];
            dl_taken_version(HB_ROOTS[r], de->d_name, hv, sizeof(hv));
            json_escape(hv, ehv, sizeof(ehv));
            n += snprintf(out + n, outsz - (size_t)n,
                          "%s{\"n\":\"%s\",\"s\":%lld,\"p\":\"%s\",\"v\":\"%s\"}",
                          n ? "," : "", de->d_name, (long long)st.st_size, full, ehv);
        }
        closedir(d);
    }
    return n;
}

static int pm_get(const char *path) {
    int s = connect_local(PLDMGR_PORT, 10000, 8000);
    if (s < 0) return -1;
    char req[700];
    int n = snprintf(req, sizeof(req),
                     "GET %s HTTP/1.0%s", path, PM_HDR_TAIL);
    write_all(s, req, (size_t)n);
    char b[1024];
    size_t got = 0;
    for (;;) {
        ssize_t r = read(s, b + got, sizeof(b) - 1 - got);
        if (r <= 0) break;
        got += (size_t)r;
        if (got >= sizeof(b) - 1) break;
        b[got] = 0;
        /* This loop exists for a reply that arrives in more than one packet, NOT to reach EOF:
           a Payload Manager that keeps the socket open after its short answer would otherwise
           hold this call - and, for install-spawn, the accept thread - for the whole 10 s
           receive timeout. Stop the moment the reply is decidable: the headers are complete and
           any body they promise has arrived, or there is no status line and it starts "OK". */
        if (!strncmp(b, "HTTP/1.", 7)) {
            const char *he = strstr(b, "\r\n\r\n");
            if (he) {
                const char *cl = strcasestr(b, "\r\ncontent-length:");
                long want = (cl && cl < he) ? strtol(cl + 17, NULL, 10) : -1;
                if (want < 0 || (long)(got - (size_t)(he + 4 - b)) >= want) break;
            }
        } else if (got >= 2 && !strncmp(b, "OK", 2)) {
            break;
        }
    }
    close(s);
    b[got] = 0;
    if (!got) return -1;
    int status = 0;
    const char *body = b;
    if (!strncmp(b, "HTTP/1.", 7)) {
        status = atoi(b + 9);
        const char *be = strstr(b, "\r\n\r\n");
        body = be ? be + 4 : b + got;
    }
    while (*body == ' ' || *body == '\r' || *body == '\n' || *body == '\t') body++;
    int ok = (status >= 200 && status < 300) || (!status && !strncmp(body, "OK", 2));
    if (!ok || strncmp(body, "OK", 2))
        ilog("pldmgr: %s -> status=%d body=%.80s", path, status, body);
    return ok ? 0 : -1;
}

/* is anything already listening on this localhost port? */
/* Which FTP the console is actually running. Elf Arsenal's ftpsrv answered on 2121; etaHEN's own
   FTP answers on 1337. Now that Arsenal is no longer bundled the port depends on what the user has
   running, so probe rather than hardcode. Returns the live port, or 0 when neither is up. */
static int ftp_live_port(void) {
    if (port_open(2121)) return 2121;
    if (port_open(1337)) return 1337;
    return 0;
}

static int port_busy(int port) {
    int s = connect_local(port, 1200, 1200);
    if (s < 0) return 0;
    close(s);
    return 1;
}

#define AUTOSTART_OFF_FLAG SHOP_DATA_DIR "/no-autostart"

static int autostart_disabled(void) {
    struct stat st;
    return stat(AUTOSTART_OFF_FLAG, &st) == 0;
}

/* ---------------- waiting until the console is actually ready ---------------
 * On a cold boot — and far more importantly on the auto-start that follows rest mode — the whole
 * payload chain comes up at once. We were asking Payload Manager to load two more payloads the
 * instant our own process began, while the system was still assembling itself. That is what made
 * the wake-from-rest crash intermittent: sometimes the shell and the network were far enough along
 * to survive it, sometimes they were not.
 *
 * Nothing below changes WHAT gets launched or in what order — only when. Three gates, each bounded
 * so a console that never reports itself ready still ends up with its helpers running:
 *   1. a delay the user can set (default AUTOSTART_DELAY_DEFAULT seconds)
 *   2. Payload Manager answering — we load through it, so it has to be up first
 *   3. a user logged in, which is the closest thing to "the PS5 has finished starting"
 * ------------------------------------------------------------------------- */
#define AUTOSTART_DELAY_FLAG    SHOP_DATA_DIR "/autostart-delay"

static int autostart_delay_secs(void) {
    int fd = open(AUTOSTART_DELAY_FLAG, O_RDONLY);
    if (fd < 0) return AUTOSTART_DELAY_DEFAULT;
    char b[16] = {0};
    int n = (int)read(fd, b, sizeof(b) - 1);
    close(fd);
    if (n <= 0) return AUTOSTART_DELAY_DEFAULT;
    int v = atoi(b);
    if (v < 0) v = 0;
    if (v > AUTOSTART_DELAY_MAX) v = AUTOSTART_DELAY_MAX;
    return v;
}

static void autostart_set_delay(int secs) {
    if (secs < 0) secs = 0;
    if (secs > AUTOSTART_DELAY_MAX) secs = AUTOSTART_DELAY_MAX;
    mkdir(SHOP_DATA_DIR, 0777);
    int fd = open(AUTOSTART_DELAY_FLAG, O_WRONLY | O_CREAT | O_TRUNC, 0644);
    if (fd < 0) return;
    char b[16];
    int n = snprintf(b, sizeof(b), "%d", secs);
    if (write(fd, b, (size_t)n) < 0) { /* nothing useful to do */ }
    close(fd);
}

/* Payload Manager answering /version. Loading a payload through a manager that is not up yet
   cannot work, so this is a precondition rather than a nicety. */
static int pm_wait_ready(int timeout_s) {
    for (int i = 0; i < timeout_s; i++) {
        long l = 0;
        char *v = http_get_ip("127.0.0.1", PLDMGR_PORT, "/version", &l);
        if (v) { free(v); return 1; }
        sleep(1);
    }
    return 0;
}

/* 1 logged in, 0 not, -1 the console will not say. Bounded by the caller either way. */
static int user_is_logged_in(void) {
    int32_t ids[4] = { -1, -1, -1, -1 };
    if (sceUserServiceGetLoginUserIdList(ids) < 0) return -1;
    for (int i = 0; i < 4; i++) if (ids[i] > 0) return 1;
    return 0;
}

static void autostart_wait_until_ready(void) {
    /* Called from two places - the tile path and the helper path. Without this guard the FULL
       user-configurable delay is served AGAIN on the second call, so a console whose tile is
       missing waits it out twice before any helper is touched. Once per boot is the intent. */
    static int done = 0;
    if (done) return;
    done = 1;
    int delay = autostart_delay_secs();
    for (int i = 0; i < delay; i++) sleep(1);          /* 1s steps: never one long opaque block */
    pm_wait_ready(90);
    for (int i = 0; i < 90; i++) {                     /* a login we cannot read must not stall us */
        int u = user_is_logged_in();
        if (u != 0) break;
        sleep(1);
    }
    sleep(2);                                          /* let the last of it settle */
}

/* Files THIS app used to write into its own payload folder and no longer ships. Removing them is
   housekeeping, not policy: they are ours, they are in our directory, and leaving a 4.7 MB copy of
   a kernel-patching daemon we never start lying around is exactly what the user asked us not to do.
   Nothing outside /data/pkg-mutant-shop is ever touched here. */
static void payload_sweep_stale(void) {
    static const char *gone[] = {
        "etaHEN.elf",              /* no longer bundled - see payload_bundle.h */
        "PKG-MUTANT-SHOP.elf",     /* an old copy of ourselves; pldmgr runs its own, not this one */
    };
    /* A build before 3.34.0 left our installer in Payload Manager's folder after every install.
       Take it back - it is put there for the two seconds a spawn needs and removed again. */
    spawn_installer_remove();
    for (unsigned i = 0; i < sizeof(gone) / sizeof(gone[0]); i++) {
        char p[300];
        snprintf(p, sizeof(p), "%s/%s", PB_DIR, gone[i]);
        if (access(p, F_OK) == 0 && unlink(p) == 0)
            ilog("boot: removed %s - no longer shipped", gone[i]);
    }
}

static void *payload_bootstrap(void *arg) {
    (void)arg;
    mkdir("/data/pkg-mutant-shop", 0777);
    mkdir(PB_DIR, 0777);
    payload_sweep_stale();
    size_t n = 0;

    /* Put our dashboard tile back if it is missing. Deliberately BEFORE the autostart checks
       below: the tile is this app's own presence on the console, not one of the helper payloads,
       so switching helpers off must not also mean "never show my app again". Registration needs
       a signed-in user, which is exactly what autostart_wait_until_ready() waits for. */
    if (!tile_is_registered()) {
        autostart_wait_until_ready();
        if (!tile_is_registered()) {
            char td[300] = {0};
            int trc = tile_install(0, td, sizeof(td));
            snprintf(g_tile_log, sizeof(g_tile_log), "%s", td);
            /* Only ever announce SUCCESS. On FW 12.70 libSceAppInstUtil does not export
               sceAppInstUtilAppInstallTitleDir at all — probed live: resolves to 0x0 by name and
               by NID, while Initialize/AppInstallPkg/AppInstallAll all resolve fine — so this
               attempt cannot succeed here and the tile is registered from the companion instead.
               Toasting that failure would mean an error popup on every single boot, which is
               worse than silence. The reason is kept in /api/tile/status for when it is wanted. */
            if (trc == 0)
                notifyf("PKG MUTANT SHOP is on your dashboard\nOpen it from Media - "
                        "you will not need to load it by hand again");
        }
    }

    /* Work out FIRST whether anything actually needs starting. Every helper prints its own
       startup notifications, which is the burst seen on the first run after a boot; on later
       runs everything is already up and nothing is launched, which is why it only happens
       once. We cannot silence another process's toasts, so instead: skip entirely when the
       user has turned autostart off, and otherwise say what is about to happen so the burst
       is expected rather than mysterious. */
    if (autostart_disabled()) {
        snprintf(g_pb_log, sizeof(g_pb_log), "autostart disabled by the user");
        return NULL;
    }
    /* Hold off until the console is genuinely up. Checked again afterwards, because the wait is
       long enough that the user may have switched autostart off in the meantime. */
    autostart_wait_until_ready();
    if (autostart_disabled()) {
        snprintf(g_pb_log, sizeof(g_pb_log), "autostart switched off while waiting");
        return NULL;
    }
    int started_any = 0;
    {
        int need = 0;
        for (int i = 0; i < PAYLOAD_BUNDLE_COUNT; i++) {
            /* BOTH FIELDS, NOT ONE. autostart is the permission and port is the evidence; a
               payload that runs without binding anything (ShadowMountPlus does exactly that) is
               otherwise a candidate to be started again on every boot for ever. */
            if (PAYLOAD_BUNDLE[i].autostart && PAYLOAD_BUNDLE[i].port > 0
                && !port_busy(PAYLOAD_BUNDLE[i].port)) need++;
        }
        if (need > 0)
            notifyf("Starting %d background service%s for game backups\n"
                    "The next few messages come from those, not from the shop",
                    need, need == 1 ? "" : "s");
    }
    for (int i = 0; i < PAYLOAD_BUNDLE_COUNT; i++) {
        const pb_entry_t *e = &PAYLOAD_BUNDLE[i];
        unsigned int len = (unsigned int)(e->end - e->data);
        char full[512];
        snprintf(full, sizeof(full), "%s/%s", PB_DIR, e->filename);

        /* write it out (refresh every boot so an updated shop ships updated helpers) */
        int fd = open(full, O_WRONLY | O_CREAT | O_TRUNC, 0777);
        int wrote = 0;
        if (fd >= 0) {
            const unsigned char *p = e->data;
            unsigned int left = len;
            while (left) {
                int w = (int)write(fd, p, left);
                if (w <= 0) break;
                p += w; left -= (unsigned int)w;
            }
            close(fd);
            wrote = (left == 0);
        }

        /* Only launch what isn't already up. ShadowMount answers on :10101 (see SMP_API_PORT -
           :9021 is elfldr and is always bound, which is why this test used to pass forever and we
           never actually started ShadowMount); the install host on :12800. Relaunching a live
           install host is exactly what wedges installs.

           port == 0 means "ship the file, never start it" — see payload_bundle.h. etaHEN is that
           case, and it matters: etaHEN rebinds :12800 on a retry loop, so if it is running and we
           also start Elf Arsenal, Arsenal's dpiv2 loses the port forever and etaHEN spins writing
           megabytes of "bind | Address already in use". Measured on 12.70. Whoever holds :12800
           first keeps it, so we start at most one install host and never a second. */
        int port = (e->autostart ? e->port : 0);
        if (port <= 0) {
            n += snprintf(g_pb_log + n, sizeof(g_pb_log) - n, "%s%s=%s",
                          n ? "," : "", e->name, wrote ? "shipped" : "write-failed");
            if (n >= sizeof(g_pb_log) - 32) break;
            continue;
        }
        int already = port_busy(port);
        int launched = 0, came_up = 0;
        if (!already) started_any = 1;
        if (wrote && !already) {
            char q[600];
            snprintf(q, sizeof(q), "/loadpayload:%s", full);
            launched = (pm_get(q) == 0);
            /* "started" used to mean only that Payload Manager ACCEPTED the request, which is not
               the same thing as the helper running. Wait for its port to actually answer, up to
               ~12 s, so the status we publish is observed instead of assumed - every downstream
               diagnosis was resting on that unverified claim. Still bounded, and it also gives
               the payload time to bind before we start the next one. */
            for (int w = 0; launched && !came_up && w < 12; w++) {
                sleep(1);
                came_up = port_busy(port);
            }
            if (!launched) sleep(1);
        }
        n += snprintf(g_pb_log + n, sizeof(g_pb_log) - n, "%s%s=%s",
                      n ? "," : "", e->name,
                      already ? "already-running"
                              : (came_up ? "started"
                                         : (launched ? "started-but-port-silent"
                                                     : (wrote ? "launch-failed" : "write-failed"))));
        if (n >= sizeof(g_pb_log) - 32) break;
    }
    /* Only speak if we actually changed something. The version toast at startup already told the
       user the shop is up; a second, unexplained toast half a minute later is noise. */
    if (started_any) {
        /* Says which of the two outcomes happened, in words that mean something to someone who
           has never heard of ShadowMount: either backups can be played from a drive, or they
           cannot. Everything else on that list was already up, which is why this is the only
           thing worth a toast. */
        if (port_open(SMP_API_PORT))
            notifyf("Game backups are ready\nPS5 backups on your drives can be started from the shop");
        else
            notifyf("Game backups are not available\nThe backup service did not start - "
                    "PKG installs still work normally");
    }
    return NULL;
}

/* ================= MUTANT CHEAT ENGINE — game memory I/O =====================
 * Reads and writes another process's memory WITHOUT ptrace (never stops the game).
 *
 * Reads go through mdbg_copyout. Writes cannot: mdbg_copyin is broken on FW 8.20+,
 * so we resolve the target virtual address to a physical page ourselves and write
 * through the kernel's direct map:
 *
 *   proc -> p_vmspace -> pmap -> {pm_pml4 (KVA), pm_cr3 (phys)}
 *   dmap_base = pm_pml4 - pm_cr3
 *   walk PML4 -> PDP -> PD -> PT (9 bits per level, 4-level x86-64)
 *   kernel_copyin(src, dmap_base + phys, n)
 *
 * This bypasses page protection entirely, so a read-only code page is writable —
 * which is exactly what a code patch needs.
 * ------------------------------------------------------------------------- */
#define PG_FRAME   0x000ffffffffff000UL
#define X86_PG_V   0x001UL
#define X86_PG_PS  0x080UL

/* Any address we touch must be canonical user-space — never kernel or MMIO. */

static unsigned long g_dmap_base = 0;

static unsigned int mem_fw(void) {
    static unsigned int c = 0;
    if (!c) c = kernel_get_fw_version() >> 16;
    return c;
}

/* Offset of pmap inside vmspace — firmware dependent. */
static unsigned long vmspace_pmap_offset(unsigned int fw) {
    if (fw >= 0x100 && fw <= 0x102) return 0x2C0;
    if (fw >= 0x105 && fw <= 0x550) return 0x2E0;
    if (fw >= 0x600 && fw <= 0x1340) return 0x2E8;   /* covers 12.70 */
    return 0;
}

static unsigned long proc_cr3(pid_t pid) {
    intptr_t proc = kernel_get_proc(pid);
    if (!proc) return 0;
    unsigned long vmspace = (unsigned long)kernel_getlong(proc + KERNEL_OFFSET_PROC_P_VMSPACE);
    if (!vmspace) return 0;
    unsigned long off = vmspace_pmap_offset(mem_fw());
    if (!off) return 0;
    /* pm_pml4 (kernel VA) and pm_cr3 (physical) are adjacent; the difference is
       the direct-map base we need for every subsequent access. */
    unsigned long d[2];
    if (kernel_copyout((intptr_t)(vmspace + off + 32), d, sizeof(d))) return 0;
    g_dmap_base = d[0] - d[1];
    return d[1];
}

static unsigned long virt2phys(unsigned long cr3, unsigned long va, unsigned long *limit) {
    unsigned long dmap = g_dmap_base;
    cr3 &= PG_FRAME;
    for (int shift = 39; shift >= 12; shift -= 9) {
        unsigned long idx = (va >> shift) & 0x1FFUL;
        cr3 = (unsigned long)kernel_getlong((intptr_t)(dmap + cr3 + idx * sizeof(unsigned long)));
        if (!(cr3 & X86_PG_V)) return (unsigned long)-1;          /* page not present */
        if ((cr3 & X86_PG_PS) || shift == 12) {                   /* large page or final level */
            cr3 &= (1UL << 52) - (1UL << shift);
            cr3 |= va & ((1UL << shift) - 1);
            if (limit) *limit = (cr3 | ((1UL << shift) - 1)) + 1;
            return cr3;
        }
        cr3 &= PG_FRAME;
    }
    return (unsigned long)-1;
}

static int mem_read(pid_t pid, intptr_t addr, void *buf, size_t len) {
    if (!ADDR_OK(addr) || !len) return -1;
    return mdbg_copyout(pid, addr, buf, len);
}

/* Write through the direct map, page by page (a run can straddle pages). */
static int mem_write(pid_t pid, intptr_t addr, const void *buf, size_t len) {
    if (!ADDR_OK(addr) || !len) return -1;
    unsigned long cr3 = proc_cr3(pid);
    if (!cr3 || !g_dmap_base) return -2;
    const unsigned char *p = (const unsigned char *)buf;
    unsigned long va = (unsigned long)addr;
    while (len) {
        unsigned long end = 0;
        unsigned long phys = virt2phys(cr3, va, &end);
        if (phys == (unsigned long)-1) return -3;
        size_t chunk = (size_t)(end - phys);
        if (chunk > len) chunk = len;
        if (kernel_copyin(p, (intptr_t)(g_dmap_base + phys), chunk)) return -4;
        va += chunk; p += chunk; len -= chunk;
    }
    return 0;
}

/* hex string -> bytes; returns count, or -1 on a malformed string */
static int hex2bytes(const char *hex, unsigned char *out, size_t cap) {
    size_t n = 0;
    const char *q = hex;
    for (;;) {
        /* .shn/.mc4 render bytes as "90-90-90-90"; JSON files have none of this, so the
           skip is a no-op there and the working JSON path is unchanged. */
        while (*q == '-' || *q == ' ' || *q == ':' || *q == ',') q++;
        if (!q[0]) break;
        /* A lone trailing digit used to be dropped on the floor: "F4240" (CUSA01140_01.16
           "Max Fuel Resources", meant as 0F 42 40) parsed as F4 24 and nobody was told. 58 such
           entries sit in 34 shipped JSON files. It is malformed, so say so - the entry is then
           counted as unreadable and the mod refuses to apply, instead of writing the wrong bytes. */
        if (!q[1]) return -1;
        int hi = hexval(q[0]), lo = hexval(q[1]);
        if (hi < 0 || lo < 0) return -1;
        if (n >= cap) return -1;
        out[n++] = (unsigned char)((hi << 4) | lo);
        q += 2;
    }
    return (int)n;
}

/* ---------------- running-game discovery (ours, no third party) -------------
 * sceSystemServiceGetAppIdOfRunningBigApp gives the foreground app id but not a pid.
 * For the pid we enumerate processes via sysctl KERN_PROC (mib 1,14,8,0): each record
 * begins with its own size and carries ki_pid at +72; for each we ask sceKernelGetAppInfo
 * and keep the one whose app_id matches. Image base comes from the dynlib map base of
 * module 0 (the eboot) rather than assuming 0x400000.
 * ------------------------------------------------------------------------- */
typedef struct { uint32_t app_id; uint64_t unknown1; char title_id[14]; char unknown2[0x3c]; } app_info_t;
int sceKernelGetAppInfo(pid_t pid, app_info_t *info);
int sceSystemServiceGetAppIdOfRunningBigApp(void);
int sceSystemServiceGetAppTitleId(int app_id, char *title_id_out);

/* JUST THE TITLE OF THE FOREGROUND GAME, cheaply, for /api/health.
 *
 * running_game() below is the full answer and is expensive: the PID it also returns can only be
 * had by enumerating every process through sysctl and calling sceKernelGetAppInfo on each one.
 * The title needs none of that - the app id names the title directly - and the title is all the
 * library grid wants in order to float the running game to the top.
 *
 * MEMOISED FOR THREE SECONDS because health is polled by every open page at once, and this is a
 * call into SceSystemService rather than a local read. Three seconds is invisible to somebody
 * launching a game and puts a hard ceiling on the cost however many screens are watching.
 *
 * Returns 0 and fills `out`, or negative with `out` empty. */
static int running_title_cached(char *out, size_t outsz) {
    static pthread_mutex_t lk = PTHREAD_MUTEX_INITIALIZER;
    static char cached[24];
    static long long at = 0;

    if (outsz) out[0] = 0;
    pthread_mutex_lock(&lk);
    long long now = now_ms_local();
    if (!at || now - at > 3000) {
        char tid[24] = {0};
        int app_id = sceSystemServiceGetAppIdOfRunningBigApp();
        if (app_id > 0 && sceSystemServiceGetAppTitleId(app_id, tid) == 0 && tid[0] &&
            ((tid[0] == 'C' && tid[1] == 'U' && tid[2] == 'S' && tid[3] == 'A') ||
             (tid[0] == 'P' && tid[1] == 'P' && tid[2] == 'S' && tid[3] == 'A')))
            snprintf(cached, sizeof(cached), "%.20s", tid);
        else
            cached[0] = 0;              /* nothing running, or a system app - both are "no game" */
        at = now;
    }
    snprintf(out, outsz, "%s", cached);
    pthread_mutex_unlock(&lk);
    return out[0] ? 0 : -1;
}

static pid_t find_pid_for_app_id(uint32_t app_id) {
    int mib[4] = {1, 14, 8, 0};
    size_t sz = 0;
    if (sysctl(mib, 4, NULL, &sz, NULL, 0) != 0 || !sz) return -1;
    uint8_t *buf = malloc(sz);
    if (!buf) return -1;
    if (sysctl(mib, 4, buf, &sz, NULL, 0) != 0) { free(buf); return -1; }
    pid_t found = -1;
    for (uint8_t *p2 = buf; p2 < buf + sz; ) {
        int rec = *(int *)p2;
        if (rec <= 0 || (size_t)rec > (size_t)((buf + sz) - p2)) break;
        if (rec < 76) { p2 += rec; continue; }        /* need ki_pid at +72 */
        pid_t pid = *(pid_t *)&p2[72];
        p2 += rec;
        app_info_t info;
        memset(&info, 0, sizeof(info));
        if (sceKernelGetAppInfo(pid, &info) == 0 && info.app_id == app_id) { found = pid; break; }
    }
    free(buf);
    return found;
}

/* 0 on success; fills the running game's title id, pid and image base. */
static int running_game(char *title, size_t tsz, pid_t *out_pid, intptr_t *out_base) {
    int app_id = sceSystemServiceGetAppIdOfRunningBigApp();
    if (app_id <= 0) return -1;
    pid_t pid = find_pid_for_app_id((uint32_t)app_id);
    if (pid <= 0) return -2;
    if (kill(pid, 0) != 0 && errno == ESRCH) return -3;   /* died between calls */
    char tid[24] = {0};
    if (sceSystemServiceGetAppTitleId(app_id, tid) != 0 || !tid[0]) {
        app_info_t info;
        memset(&info, 0, sizeof(info));
        if (sceKernelGetAppInfo(pid, &info) != 0 || !info.title_id[0]) return -4;
        snprintf(tid, sizeof(tid), "%.13s", info.title_id);
    }
    /* real games only, not system apps */
    if (!((tid[0] == 'C' && tid[1] == 'U' && tid[2] == 'S' && tid[3] == 'A') ||
          (tid[0] == 'P' && tid[1] == 'P' && tid[2] == 'S' && tid[3] == 'A'))) return -5;
    if (title && tsz) snprintf(title, tsz, "%s", tid);
    if (out_pid) *out_pid = pid;
    if (out_base) {
        intptr_t b = kernel_dynlib_mapbase_addr(pid, 0);
        *out_base = b ? b : 0x400000;
    }
    return 0;
}

/* ================= MUTANT CHEAT ENGINE — cheat file parsing ==================
 * Cheat JSON shape (verified against real files on this console):
 *   { "id":"CUSA14409", "version":"01.04", "process":"eboot.bin",
 *     "mods":[ { "name":"...", "type":"checkbox",
 *                "memory":[ {"offset":"183980","on":"41..","off":"00.."} ] } ] }
 *
 * Applying a mod writes each entry's "on" bytes at eboot_base + offset; reverting
 * writes "off". Both are gated on the memory currently holding the OTHER state, so a
 * cheat built for a different build cannot scribble over unrelated code.
 *
 * Hand-rolled scanning rather than a JSON library: the schema is tiny and fixed, and
 * this keeps the payload dependency-free.
 * ------------------------------------------------------------------------- */
/* Read a whole file into a heap buffer (caller frees). */
static char *slurp(const char *path, long *out_len) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return NULL;
    struct stat st;
    if (fstat(fd, &st) != 0 || st.st_size <= 0 || st.st_size > (8 << 20)) { close(fd); return NULL; }
    char *buf = malloc((size_t)st.st_size + 1);
    if (!buf) { close(fd); return NULL; }
    long n = 0;
    while (n < st.st_size) {
        ssize_t r = read(fd, buf + n, (size_t)(st.st_size - n));
        if (r <= 0) break;
        n += r;
    }
    close(fd);
    buf[n] = 0;
    if (out_len) *out_len = n;
    return buf;
}

/* Copy the string value of "key":"value" starting at or after p. Returns end ptr or NULL. */
/* BOUNDED, because an unbounded one reads the next object's key as this one's.
 *
 * Measured: a master block documents only `on` bytes, and asking for its "off" with a plain strstr
 * from the entry's start walked past the whole master object into the first mod's entry and returned
 * ITS off bytes - so a master the file says cannot be removed was "removed", into the wrong address.
 * Every one of the 25,888 mod entries in the shipped library carries all three keys, which is why
 * only the master blocks exposed it.
 *
 * `end` may be NULL, which means "no bound" and is exactly the old behaviour. */
static const char *json_str_after_lim(const char *p, const char *end, const char *key,
                                      char *out, size_t outsz) {
    char pat[48];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *k = strstr(p, pat);
    if (!k) return NULL;
    if (end && k >= end) return NULL;                 /* it belongs to something after us */
    k += strlen(pat);
    /* Step over exactly `: ` and nothing else. Scanning ahead for the next quote — the old
       behaviour — meant a null value walked straight into the FOLLOWING key and returned its
       name as the value: `"version":null,"items":[...]` read back as "items". Anything that is
       not a quoted string (null, a number, an object, an array) has no string to give. */
    while (*k == ' ' || *k == '\t' || *k == '\n' || *k == '\r') k++;
    if (*k != ':') return NULL;
    k++;
    while (*k == ' ' || *k == '\t' || *k == '\n' || *k == '\r') k++;
    if (*k != '"') return NULL;
    k++;
    size_t j = 0;
    while (*k && *k != '"' && j < outsz - 1) {
        if (*k == 0x5C && k[1]) {
            /* Decode the escape rather than merely dropping the backslash. Dropping it turned the
               ™ that Python's json emits for "Star Wars™" into the literal text "u2122", so the
               console showed "Star Warsu2122: Racer Revengeu2122". */
            k++;
            char e = *k++;
            if (e == 'u') {
                unsigned cp = 0;
                int got = 0;
                for (; got < 4 && k[got]; got++) {
                    char h = k[got];
                    unsigned v;
                    if (h >= '0' && h <= '9') v = (unsigned)(h - '0');
                    else if (h >= 'a' && h <= 'f') v = (unsigned)(h - 'a' + 10);
                    else if (h >= 'A' && h <= 'F') v = (unsigned)(h - 'A' + 10);
                    else break;
                    cp = (cp << 4) | v;
                }
                if (got == 4) {
                    k += 4;
                    if (cp < 0x80) { if (j < outsz - 1) out[j++] = (char)cp; }
                    else if (cp < 0x800) {
                        if (j < outsz - 2) { out[j++] = (char)(0xC0 | (cp >> 6));
                                             out[j++] = (char)(0x80 | (cp & 0x3F)); }
                    } else {
                        if (j < outsz - 3) { out[j++] = (char)(0xE0 | (cp >> 12));
                                             out[j++] = (char)(0x80 | ((cp >> 6) & 0x3F));
                                             out[j++] = (char)(0x80 | (cp & 0x3F)); }
                    }
                } else {
                    k += got;                  /* malformed \u: drop its digits too, so no "12" leaks */
                }
                continue;
            }
            if (e == 'n' || e == 't' || e == 'r') out[j++] = ' ';
            else out[j++] = e;                 /* \" \\ \/ and anything else: the char itself */
            continue;
        }
        out[j++] = *k++;
    }
    out[j] = 0;
    return (*k == '"') ? k + 1 : NULL;
}

static const char *json_str_after(const char *p, const char *key, char *out, size_t outsz) {
    return json_str_after_lim(p, NULL, key, out, outsz);
}

/* Find the substring covering mods[index]; returns start ptr and sets *end. */
static const char *find_mod_block(const char *json, int index, const char **end) {
    const char *m = strstr(json, "\"mods\"");
    if (!m) return NULL;
    m = strchr(m, '[');
    if (!m) return NULL;
    /* SAME WALK AS next_mod_block, AND FOR THE SAME REASON: a brace inside a cheat's name is text,
       not structure, and counting it shifts every block after it. This one is worse than the other if
       it drifts, because it is indexed - a cheat is applied BY INDEX, so a shifted boundary applies a
       different cheat than the one that was pressed. */
    int depth = 0, cur = -1, instr = 0;
    const char *start = NULL;
    for (const char *q = m; *q; q++) {
        if (instr) {
            if (*q == 0x5C && q[1]) { q++; continue; }
            if (*q == '"') instr = 0;
            continue;
        }
        if (*q == '"') { instr = 1; continue; }
        if (*q == '{') { if (depth == 0) { cur++; start = q; } depth++; }
        else if (*q == '}') {
            depth--;
            if (depth == 0 && cur == index) { *end = q + 1; return start; }
        } else if (*q == ']' && depth == 0) break;
    }
    return NULL;
}

/* Just past the '[' of "mods", or NULL. Feed it to next_mod_block(). */
static const char *mods_array_start(const char *json) {
    const char *m = strstr(json, "\"mods\"");
    if (!m) return NULL;
    m = strchr(m, '[');
    return m ? m + 1 : NULL;
}

/* The mod block that begins after `from` (the '[' of "mods", or the end of the previous block).
   find_mod_block() walks the document from the top for every index, which made listing a
   96-mod file - and Disable-all over one - quadratic in the file size, on the accept loop. Same
   brace rules as find_mod_block, so the two agree on what block N is. */
/* STRINGS ARE NOT STRUCTURE. This counted every brace it saw, including the ones inside a cheat's
 * NAME - and the shipped library has ten of those. Nine are balanced and survived by luck;
 * CUSA29102_01.01's "Max Items {after using have 2)" is not, and it cost a real cheat: the engine
 * walked 4 of that file's 5 mods, with "Max experience" swallowed into the block before it. A mod
 * whose block has eaten its neighbour applies BOTH memory arrays - so pressing one cheat wrote
 * another one the owner never touched.
 *
 * cheat_master_span already skipped strings for exactly this reason; this is the same walk. */
static const char *next_mod_block(const char *from, const char **end) {
    const char *q = from;
    while (*q && *q != '{' && *q != ']') q++;
    if (*q != '{') return NULL;
    const char *start = q;
    int depth = 0, instr = 0;
    for (; *q; q++) {
        if (instr) {
            if (*q == 0x5C && q[1]) { q++; continue; }      /* an escape: skip what it escapes */
            if (*q == '"') instr = 0;
            continue;
        }
        if (*q == '"') { instr = 1; continue; }
        if (*q == '{') depth++;
        else if (*q == '}') { depth--; if (depth == 0) { *end = q + 1; return start; } }
    }
    return NULL;
}

/* "dropped":N inside one mod block - shn_xml_to_json writes it for cheatlines it could not copy
   out. Bounded to the block: a plain strstr from `blk` would read the NEXT mod's count. */
static int mod_dropped_count(const char *blk, const char *blk_end) {
    const char *d = strstr(blk, "\"dropped\"");
    if (!d || d >= blk_end) return 0;
    d += 9;
    while (*d == ' ' || *d == ':') d++;
    int v = atoi(d);
    return v > 0 ? v : 0;
}

/* Parse the memory[] array of one mod block. Returns entry count. */
static int parse_mod_entries(const char *blk, const char *blk_end, cheat_entry_t *out, int max) {
    return parse_mod_entries_ex(blk, blk_end, out, max, NULL);
}

/* The same, and via *dropped_out how many entries the engine COULD NOT read: hex longer than
   CHEAT_MAX_BYTES, an odd digit count, a value cut short by the text buffer, an entry beyond
   CHEAT_MAX_ENTRIES, plus the cheatlines the XML converter already gave up on. Those used to
   vanish - the entry was skipped and the rest of the mod applied - and a hook missing one of its
   parts is how a game hangs. Now they are counted, the mod refuses to apply, and its state says
   so. */
static int parse_mod_entries_ex(const char *blk, const char *blk_end, cheat_entry_t *out, int max,
                                int *dropped_out) {
    int dropped = mod_dropped_count(blk, blk_end);
    const char *mem = strstr(blk, "\"memory\"");
    if (!mem || mem >= blk_end) { if (dropped_out) *dropped_out = dropped; return 0; }
    /* Hex text buffers: 2 chars per byte plus slack, so a value that overruns them is provably
       longer than CHEAT_MAX_BYTES. Heap - at 4 KB per entry they no longer belong on a stack.
       Not static: the HTTP server is threaded and these would race. */
    size_t hsz = (size_t)CHEAT_MAX_BYTES * 2 + 16;
    char *onh = (char *)malloc(hsz), *offh = (char *)malloc(hsz);
    if (!onh || !offh) { free(onh); free(offh); if (dropped_out) *dropped_out = dropped; return 0; }
    int n = 0, depth = 0;
    const char *s2 = NULL;
    for (const char *q = mem; q < blk_end && *q; q++) {
        if (*q == '{') { if (depth == 0) s2 = q; depth++; }
        else if (*q == '}') {
            depth--;
            if (depth == 0 && s2) {
                char off[40] = {0};
                onh[0] = 0; offh[0] = 0;
                /* BOUNDED BY THIS ENTRY (q is its closing brace). Unbounded, a key this entry does
                   not have was answered by the next object that does - which is how a master block
                   with no documented original bytes acquired another mod's. */
                json_str_after_lim(s2, q, "offset", off, sizeof(off));
                /* NULL with text already copied means the value did not fit the buffer. The old
                   code went on to parse that truncated hex as a shorter, wrong-length write. */
                const char *ron  = json_str_after_lim(s2, q, "on",  onh,  hsz);
                const char *roff = json_str_after_lim(s2, q, "off", offh, hsz);
                if (off[0]) {
                    int bad = (onh[0] && !ron) || (offh[0] && !roff);
                    if (n >= max) bad = 1;                    /* beyond the table: not silently lost */
                    cheat_entry_t *e = bad ? NULL : &out[n];
                    int on_len = 0, off_len = 0;
                    if (e) {
                        memset(e, 0, sizeof(*e));
                        e->offset = strtoull(off, NULL, 16);      /* offsets are hex, no 0x prefix */
                        on_len  = onh[0]  ? hex2bytes(onh,  e->on,  sizeof(e->on))  : 0;
                        off_len = offh[0] ? hex2bytes(offh, e->off, sizeof(e->off)) : 0;
                        if (on_len < 0 || off_len < 0) bad = 1;   /* too long, or malformed hex */
                    }
                    if (bad) dropped++;
                    else {
                        e->on_len = on_len; e->off_len = off_len;
                        /* SECTION: which loaded module the offset belongs to. Zero or absent
                           means the main executable, the only one this engine places. Every one of
                           the 306 entries in the shipped library quotes it ("section": "11"), so a
                           bare strtol after the colon stops on the quote and reads 0 - which would
                           be a check that can never fire. Both forms are accepted. */
                        const char *sc = strstr(s2, "\"section\"");
                        if (sc && sc < q) {
                            const char *sv = strchr(sc, ':');
                            if (sv && sv < q) {
                                sv++;
                                /* A NEWLINE IS WHITESPACE TOO. Every entry in the shipped library is
                                   on one line, so nothing breaks today - but a pretty-printed file
                                   would read section 0 and be applied at base + offset, which is
                                   exactly the write the section refusal exists to stop. */
                                while (sv < q && (*sv == ' ' || *sv == '\t' || *sv == '\n' ||
                                                  *sv == '\r' || *sv == '"')) sv++;
                                e->section = (int)strtol(sv, NULL, 10);
                            }
                        }
                        /* .shn/.mc4 can mark an individual offset as already absolute. */
                        const char *ab = strstr(s2, "\"absolute\"");
                        if (ab && ab < q) {
                            const char *tv = strstr(ab, "true");
                            if (tv && tv < q) e->absolute = 1;
                        }
                        if (e->on_len > 0 || e->off_len > 0) n++;
                    }
                }
                s2 = NULL;
            }
        } else if (*q == ']' && depth == 0) break;
    }
    free(onh); free(offh);
    if (dropped_out) *dropped_out = dropped;
    return n;
}

/* Is `cur` a state some mod in this file legitimately puts at `offset`?
 * Mods often share a hook address with different jump targets (that is what makes them
 * mutually exclusive). Verifying only against THIS mod's on/off would refuse the write
 * whenever a sibling owns the hook, leaving the mod half-applied. Accepting any documented
 * state keeps us safe (we still never write over unrecognised code) while allowing a clean
 * hand-over. Returns the index of the mod whose "on" bytes are present, or -1. */
static int offset_known_state(const char *json, unsigned long long offset,
                              const unsigned char *cur, int len, int *out_owner) {
    int known = 0;
    if (out_owner) *out_owner = -1;
    cheat_entry_t *es = (cheat_entry_t *)malloc(CHEAT_ENTS_BYTES);
    if (!es) return 0;
    const char *from = mods_array_start(json);
    for (int mi = 0; from && mi < CHEAT_MAX_MODS; mi++) {
        const char *end = NULL;
        const char *blk = next_mod_block(from, &end);
        if (!blk) break;
        from = end;
        int n = parse_mod_entries(blk, end, es, CHEAT_MAX_ENTRIES);
        for (int i = 0; i < n; i++) {
            if (es[i].offset != offset) continue;
            if (es[i].on_len == len && memcmp(cur, es[i].on, (size_t)len) == 0) {
                known = 1;
                if (out_owner) *out_owner = mi;
            }
            if (es[i].off_len == len && memcmp(cur, es[i].off, (size_t)len) == 0) known = 1;
        }
    }
    free(es);
    return known;
}

/* Report a mod's live state by reading memory: "on" (every entry matches its on-bytes),
 * "off" (every entry matches off), "partial" (a mix — usually a displaced shared hook),
 * or "unknown" (memory matches neither, e.g. wrong game build).
 * Without this the UI has to guess, which made toggles appear to flip back by themselves.
 * Takes the mod's block: the listing routes walk the document once with next_mod_block() and
 * hand each block here, where the by-index form (find_mod_block from the top, per mod) used to
 * rescan the whole file for every row. */
static const char *cheat_mod_state_blk(const char *json, const char *blk, const char *end,
                                       pid_t pid, intptr_t base, int non_json) {
    (void)json;
    cheat_entry_t *es = (cheat_entry_t *)malloc(CHEAT_ENTS_BYTES);
    if (!es) return "unknown";
    int dropped = 0;
    int n = parse_mod_entries_ex(blk, end, es, CHEAT_MAX_ENTRIES, &dropped);
    if (n <= 0) { free(es); return "unknown"; }
    int abs_mode = non_json ? cheat_addr_mode(es, n, pid, base) : 0;
    int on = 0, off = 0, other = 0;
    for (int i = 0; i < n; i++) {
        int len = es[i].on_len ? es[i].on_len : es[i].off_len;
        if (len <= 0) continue;
        unsigned char cur[CHEAT_MAX_BYTES];
        intptr_t addr = cheat_entry_addr(&es[i], base, abs_mode);
        if (!ADDR_OK(addr) || mem_read(pid, addr, cur, (size_t)len) != 0) { other++; continue; }
        if (es[i].on_len == len && memcmp(cur, es[i].on, (size_t)len) == 0) on++;
        else if (es[i].off_len == len && memcmp(cur, es[i].off, (size_t)len) == 0) off++;
        else other++;
    }
    free(es);
    /* A mod with entries the engine could not read is never whole. Its readable part being ON
       means only that part was ever written (by force, or by an older build), so it is "partial";
       otherwise it is "unknown", which greys the tile out up front - the same refusal the apply
       would give (cheat_apply_blk). It used to report "on" for the part it could see. */
    if (dropped > 0) return on ? "partial" : "unknown";
    if (on && !off && !other) return "on";
    if (off && !on && !other) return "off";
    if (on || off) return "partial";
    return "unknown";
}

/* Apply (want_on=1) or revert (0) one mod. Writes each entry only when memory currently
   holds the opposite state — unless force. Returns entries written; negatives are errors.
   Three layers: by file (this), by loaded document, by block - so a route that already holds
   the document (Disable-all, the listings) never re-reads and re-decrypts it per mod. */
static int cheat_apply_mod(const char *file, int index, int want_on, pid_t pid, intptr_t base,
                           int force, int check_only, char *detail, size_t dsz) {
    int non_json = 0;
    char *json = cheat_load_doc(file, &non_json);      /* .json / .shn / .mc4 all land here */
    if (!json) { snprintf(detail, dsz, "cannot read %s", file); return -1; }
    int rc = cheat_apply_mod_doc(json, non_json, index, want_on, pid, base, force, check_only,
                                 detail, dsz);
    free(json);
    return rc;
}

static int cheat_apply_mod_doc(const char *json, int non_json, int index, int want_on, pid_t pid,
                               intptr_t base, int force, int check_only, char *detail, size_t dsz) {
    const char *end = NULL;
    const char *blk = find_mod_block(json, index, &end);
    if (!blk) { snprintf(detail, dsz, "mod %d not found", index); return -2; }
    return cheat_apply_blk(json, non_json, blk, end, index, want_on, pid, base, force, check_only,
                           detail, dsz);
}


/* ================ SIGNATURE SEARCH ============================================================
 * See the note on the routes: this is the primitive porting, the mask patch lines and "find an
 * address from scratch" all need, and none of them can exist without it.
 *
 * ONE AT A TIME, IN A THREAD, AND READ-ONLY. mem_read is the agent's file channel on a PS4 (4 KB per
 * request at a 25 ms poll, so ~3 minutes for a 34 MB module) and a page-table walk on a PS5 (seconds).
 * The nap between chunks is deliberate: that same channel carries every cheat toggle and every state
 * read, and a sweep that starved it would make the shop look broken while it ran.
 * ============================================================================================ */
#define SIG_MAX       64      /* bytes in a signature - the longest mask in the patch library is ~40 */
#define SIG_HITS_MAX  64
#define SIG_CHUNK     (16 * 1024)

typedef struct pms_sig { unsigned char b[SIG_MAX], m[SIG_MAX]; int n; } pms_sig_t;

/* ITS OWN CLOCK, because the two payloads do not agree on the name of theirs - the PS4 has now_ms()
   and this file has now_ms_local(), and shared code that picked either would fail to build in the
   other. Four lines is cheaper than a shim. */
static long long sig_now_ms(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (long long)ts.tv_sec * 1000 + ts.tv_nsec / 1000000;
}

/* "48 8B 05 ?? ?? ?? ?? 89" -> bytes + a mask, where m[i]==0 means "anything here". Accepts spaces,
   dashes and commas as separators because all three appear in the wild (the Trainer format uses
   dashes). The wildcard is two question marks, two asterisks or two x's. Returns the byte count, or 0
   when it is not a usable signature. */
static int sig_parse(const char *t, pms_sig_t *s) {
    if (!t || !s) return 0;
    memset(s, 0, sizeof(*s));
    int n = 0;
    for (const char *p = t; *p && n < SIG_MAX; ) {
        if (*p == ' ' || *p == '-' || *p == ',' || *p == ':' || *p == '\t') { p++; continue; }
        if ((p[0] == '?' || p[0] == '*' || p[0] == 'x' || p[0] == 'X') && p[1] == p[0]) {
            s->b[n] = 0; s->m[n] = 0; n++; p += 2; continue;
        }
        int hi = hexval(p[0]), lo = p[1] ? hexval(p[1]) : -1;
        if (hi < 0 || lo < 0) return 0;                  /* not hex and not a wildcard: refuse */
        s->b[n] = (unsigned char)((hi << 4) | lo);
        s->m[n] = 1;
        n++; p += 2;
    }
    s->n = n;
    /* A signature of nothing but wildcards matches everywhere, which is not a search. */
    int real = 0;
    for (int i = 0; i < n; i++) if (s->m[i]) real++;
    return (n >= 4 && real >= 3) ? n : 0;
}

static struct sig_job {
    pthread_mutex_t lock;
    int       running, cancel, ever;
    pid_t     pid;
    intptr_t  base;
    long long from, to, cur;
    pms_sig_t sig;
    char      pat[200];
    long long hits[SIG_HITS_MAX];
    int       nhits, truncated, unreadable;
    long long started_ms, ended_ms;
} g_sig = { PTHREAD_MUTEX_INITIALIZER, 0, 0, 0, 0, 0, 0, 0, 0, {{0}, {0}, 0}, {0}, {0}, 0, 0, 0, 0, 0 };

static void *sig_thread(void *arg) {
    (void)arg;
    unsigned char *buf = (unsigned char *)malloc(SIG_CHUNK);
    if (!buf) {
        pthread_mutex_lock(&g_sig.lock);
        g_sig.running = 0; g_sig.ended_ms = sig_now_ms();
        pthread_mutex_unlock(&g_sig.lock);
        return NULL;
    }
    for (;;) {
        pthread_mutex_lock(&g_sig.lock);
        int stop = g_sig.cancel || g_sig.cur >= g_sig.to;
        long long cur = g_sig.cur, to = g_sig.to;
        pid_t pid = g_sig.pid;
        intptr_t base = g_sig.base;
        pms_sig_t sig = g_sig.sig;
        pthread_mutex_unlock(&g_sig.lock);
        if (stop) break;

        long long want = to - cur;
        if (want > SIG_CHUNK) want = SIG_CHUNK;
        /* A FAILED READ IS A GAP, NOT THE END. A module's range has holes between its segments, and
           the agent refuses anything outside them - so skip the chunk, count it, and carry on. */
        int bad = (mem_read(pid, base + (intptr_t)cur, buf, (size_t)want) != 0);
        long long found[SIG_HITS_MAX];
        int nf = 0, over = 0;
        if (!bad) {
            for (long long i = 0; i + sig.n <= want; i++) {
                int ok = 1;
                for (int k = 0; k < sig.n; k++)
                    if (sig.m[k] && buf[i + k] != sig.b[k]) { ok = 0; break; }
                if (!ok) continue;
                /* THE CAP HAS TO BE VISIBLE. This used to stop the loop at SIG_HITS_MAX and only mark
                   `truncated` when the shared list was full - so a chunk with more matches than fit
                   dropped the rest in silence, and the status said the list was complete. A porting
                   tool reading "1 match" from a truncated list would place a cheat at the first of
                   many. */
                if (nf < SIG_HITS_MAX) found[nf++] = cur + i; else over = 1;
            }
        }
        /* OVERLAP BY n-1, so a match lying across a chunk boundary is found - and found ONCE, because
           a match starting inside the overlap cannot complete in the earlier chunk. */
        long long step = want - (sig.n - 1);
        if (step < 1) step = want;

        pthread_mutex_lock(&g_sig.lock);
        if (bad) g_sig.unreadable++;
        if (over) g_sig.truncated = 1;            /* more in this chunk than the chunk list could hold */
        for (int i = 0; i < nf; i++) {
            if (g_sig.nhits < SIG_HITS_MAX) g_sig.hits[g_sig.nhits++] = found[i];
            else g_sig.truncated = 1;
        }
        g_sig.cur = cur + step;
        pthread_mutex_unlock(&g_sig.lock);
        usleep(1000);
    }
    free(buf);
    pthread_mutex_lock(&g_sig.lock);
    g_sig.running = 0;
    g_sig.ended_ms = sig_now_ms();
    pthread_mutex_unlock(&g_sig.lock);
    return NULL;
}

/* The most a single search will sweep. Nothing legitimate asks for more - the largest module measured
   here is 34 MB - and without it a mistyped `to` spends an hour reading gaps. */
#define SIG_SPAN_MAX  ((long long)256 << 20)

/* 0 started, -1 one is already running, -2 not a usable signature, -3 the range makes no sense,
   -4 the thread would not start, -5 there is no game to search. */
static int sig_start(const char *pattern, pid_t pid, intptr_t base, long long from, long long to) {
    pms_sig_t parsed;
    if (sig_parse(pattern, &parsed) <= 0) return -2;
    if (to <= from || from < 0) return -3;
    if (to - from > SIG_SPAN_MAX) return -3;
    /* NO GAME, NO SEARCH. Without this, a pid of 0 spends thirty-five seconds failing every read and
       reports "0 found" - which reads as "those bytes are not in the game" rather than "nobody
       looked". A wrong answer delivered confidently is the thing this project keeps paying for. */
    if (pid <= 0 || base <= 0) return -5;
    pthread_mutex_lock(&g_sig.lock);
    if (g_sig.running) { pthread_mutex_unlock(&g_sig.lock); return -1; }
    g_sig.sig = parsed;
    snprintf(g_sig.pat, sizeof(g_sig.pat), "%s", pattern ? pattern : "");
    g_sig.pid = pid; g_sig.base = base;
    g_sig.from = from; g_sig.to = to; g_sig.cur = from;
    g_sig.nhits = 0; g_sig.truncated = 0; g_sig.unreadable = 0;
    g_sig.cancel = 0; g_sig.running = 1; g_sig.ever = 1;
    g_sig.started_ms = sig_now_ms(); g_sig.ended_ms = 0;
    pthread_mutex_unlock(&g_sig.lock);

    pthread_t t;
    pthread_attr_t a;
    pthread_attr_init(&a);
    pthread_attr_setdetachstate(&a, PTHREAD_CREATE_DETACHED);
    if (pthread_create(&t, &a, sig_thread, NULL) != 0) {
        pthread_attr_destroy(&a);
        pthread_mutex_lock(&g_sig.lock);
        g_sig.running = 0;
        pthread_mutex_unlock(&g_sig.lock);
        return -4;
    }
    pthread_attr_destroy(&a);
    return 0;
}

static void sig_cancel(void) {
    pthread_mutex_lock(&g_sig.lock);
    g_sig.cancel = 1;
    pthread_mutex_unlock(&g_sig.lock);
}

static void sig_status_json(char *out, size_t osz) {
    pthread_mutex_lock(&g_sig.lock);
    char esc[400];
    json_escape(g_sig.pat, esc, sizeof(esc));
    long long span = g_sig.to - g_sig.from;
    long long done = g_sig.cur - g_sig.from;
    if (done < 0) done = 0;
    if (span > 0 && done > span) done = span;
    size_t l = (size_t)snprintf(out, osz,
        "{\"ok\":true,\"ever\":%s,\"active\":%s,\"pattern\":\"%s\",\"pid\":%d,\"base\":\"0x%llx\","
        "\"from\":%lld,\"to\":%lld,\"scanned\":%lld,\"percent\":%d,\"found\":%d,"
        "\"truncated\":%s,\"gaps\":%d,\"ms\":%lld,\"offsets\":[",
        g_sig.ever ? "true" : "false", g_sig.running ? "true" : "false", esc, (int)g_sig.pid,
        (unsigned long long)g_sig.base, g_sig.from, g_sig.to, done,
        span > 0 ? (int)((done * 100) / span) : 0, g_sig.nhits,
        g_sig.truncated ? "true" : "false", g_sig.unreadable,
        (g_sig.ended_ms ? g_sig.ended_ms : sig_now_ms()) - g_sig.started_ms);
    for (int i = 0; i < g_sig.nhits && l < osz - 32; i++)
        l += (size_t)snprintf(out + l, osz - l, "%s\"%llX\"", i ? "," : "",
                              (unsigned long long)g_sig.hits[i]);
    if (l < osz - 4) snprintf(out + l, osz - l, "]}");
    pthread_mutex_unlock(&g_sig.lock);
}

/* ================ THE MASTER CODE ("Must Be On") ==============================================
 *
 * Some trainers are built in two pieces: a master block that installs a shared routine and the
 * scratch it works through, and then cheats that are diffs against it. Both formats we read carry
 * one, and the engine read neither - mods are found only inside the "mods" array.
 *
 *   json   "master": { "challenged": "yes", "memory": [ {offset, on}, ... ] }
 *   shn    <StartUP Text="Master Code 1 (Must Be On)"> <Cheatline>...</Cheatline> </StartUP>
 *
 * MEASURED, on the file we ship for Dark Souls II (CUSA01589 01.02) and on the running game. Its
 * "1 hit kill" installs a cave that does `cmp r15, [rip-0x2473876]` from +0x207786F, and the master's
 * cave does `mov [rip-0x2473807], rbx` from +0x2077807. Both resolve to the same qword: base -
 * 0x3FC000, absolute 0x4000 - which the agent reports as NOT inside any module this process has
 * loaded. So the slot the whole trainer works through does not exist, the game faults the first time
 * either routine runs, and that is both of the crashes the owner saw: on hitting an enemy (the damage
 * path) and instantly (the master's hook, which is in a path that runs constantly).
 *
 * The master is applied anyway when it CAN be - 17 of the 20 files carry no such operand - because a
 * cave-resident cheat cannot work without it. run_unreachable() is what separates the two cases.
 *
 * Applied whole or not at all, through the same gate a mod uses, and skipped entry by entry when it
 * is already in place - so the second cheat in a session costs four reads and no writes.
 * ============================================================================================ */
static const char *cheat_master_span(const char *json, const char **end) {
    /* The "master" object, or NULL. Brace-matched with strings skipped: a cheat name can contain a
       brace, and a span that ends early would hand parse_mod_entries_ex half an array. */
    if (!json) return NULL;
    /* THE KEY, NOT THE WORD. "master" appears in cheat NAMES all over the library - "[ENG] Master
       Code", "Player Inv. Master Code", twenty-one of them - and the first '{' after any of those
       belongs to something else entirely. A key is followed by a colon. */
    const char *m = json, *o = NULL;
    while ((m = strstr(m, "\"master\"")) != NULL) {
        const char *q = m + 8;
        while (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r') q++;
        if (*q == ':') {
            q++;
            while (*q == ' ' || *q == '\t' || *q == '\n' || *q == '\r') q++;
            if (*q == '{') { o = q; break; }
        }
        m += 8;
    }
    if (!o) return NULL;
    int depth = 0, instr = 0;
    for (const char *q = o; *q; q++) {
        if (instr) {
            if (*q == '\\' && q[1]) { q++; continue; }
            if (*q == '"') instr = 0;
            continue;
        }
        if (*q == '"') { instr = 1; continue; }
        if (*q == '{') depth++;
        else if (*q == '}') {
            depth--;
            if (depth == 0) { if (end) *end = q + 1; return o; }
        }
    }
    return NULL;
}

/* ---- WHAT A RUN OF CODE REACHES --------------------------------------------------------------
 *
 * A cheat that installs a routine brings its own code, and that code can touch memory the game does
 * not have. Dark Souls II's master does `mov [rip-0x2473807], rbx` from image offset +0x2077807,
 * which is image base - 0x3FC000, i.e. absolute 0x4000 on these consoles - not inside any module the
 * process has loaded. The game faults the first time that path runs, which is exactly the crash the
 * owner reported for "1 hit kill" (on hitting an enemy) and then for the master itself (instantly).
 *
 * So before writing a run, resolve the RIP-relative operands in it and refuse if any of them points
 * somewhere we cannot even read.
 *
 * THE DECODE IS NARROW ON PURPOSE. It is not a disassembler: it looks for the exact prefixes that
 * appear in real cheat files and reads the disp32 that follows. Measured over the whole shipped
 * library - 8,126 cave mods and 20 master blocks - it finds 265 operands, 260 of which resolve to a
 * non-negative image offset and five of which resolve to exactly -0x3FC000, in three files. Five
 * identical values is the authoring tool's convention, not decoder noise. A false positive here
 * refuses a cheat that might have worked; a false negative crashes a game. */
#define RIP_MAX_OPS 24
static const unsigned char RIP_PFX[][3] = {
    {0x4C,0x3B,0x3D}, {0x48,0x8B,0x05}, {0x48,0x8D,0x05}, {0x48,0x3B,0x05},
    {0x4C,0x8D,0x3D}, {0x48,0x8B,0x0D}, {0x48,0x89,0x1D}, {0x48,0x8B,0x1D},
    {0x48,0x89,0x05}, {0x48,0x8B,0x15}, {0x48,0x89,0x0D}, {0x48,0x89,0x15},
};

static long long rip_disp32(const unsigned char *p) {
    unsigned int v = (unsigned int)p[0] | ((unsigned int)p[1] << 8)
                   | ((unsigned int)p[2] << 16) | ((unsigned int)p[3] << 24);
    return (long long)(int)v;
}

/* Where the operands in b[0..n) point, given that b[0] sits at the ABSOLUTE address run_addr.
 *
 * POSITION-MAJOR, and that is not a style choice. It used to loop prefixes on the outside under a
 * shared `got < max` cap, so a run with two dozen `4C 3B 3D` operands filled every slot and the later
 * prefixes - including the `48 89 1D` that writes Dark Souls II's scratch - were never looked at. A
 * safety check whose whole job is not to miss one cannot be allowed to skip a whole kind. */
static int rip_targets(const unsigned char *b, int n, intptr_t run_addr, intptr_t *out, int max) {
    int got = 0;
    for (int i = 0; i + 7 <= n && got < max; i++) {
        for (unsigned k = 0; k < sizeof(RIP_PFX) / sizeof(RIP_PFX[0]); k++) {
            if (b[i] != RIP_PFX[k][0] || b[i+1] != RIP_PFX[k][1] || b[i+2] != RIP_PFX[k][2]) continue;
            out[got++] = run_addr + i + 7 + (intptr_t)rip_disp32(b + i + 3);   /* prefix(3)+disp32(4) */
            break;                                    /* one operand per position, whichever matched */
        }
    }
    return got;
}

/* 1 when this run reaches memory the game does not have, and *bad is the offending ADDRESS.
 *
 * ABSOLUTE, throughout. It used to take an image offset and add `base` - which is wrong for every
 * .shn and .mc4 entry that cheat_addr_mode decided was already absolute, and for any entry carrying
 * <Absolute>: the operand was resolved from a place the run is not at. The caller knows the real
 * address, because it just read it.
 *
 * THE TARGET IS DECIDED BY READING IT, not by arithmetic about where zero is - and read TWICE before
 * it is believed. On a PS4 every read is a request over the agent's file channel, and one lost
 * request must not turn into a permanent verdict about a cheat file. */
static int run_unreachable(const unsigned char *b, int n, intptr_t run_addr,
                           pid_t pid, intptr_t *bad) {
    intptr_t t[RIP_MAX_OPS];
    int got = rip_targets(b, n, run_addr, t, RIP_MAX_OPS);
    for (int i = 0; i < got; i++) {
        unsigned char one;
        if (!ADDR_OK(t[i])) { if (bad) *bad = t[i]; return 1; }
        if (mem_read(pid, t[i], &one, 1) == 0) continue;
        if (mem_read(pid, t[i], &one, 1) == 0) continue;
        if (bad) *bad = t[i];
        return 1;
    }
    return 0;
}

/* ---- THE MASTER, DECIDED BEFORE IT IS WRITTEN ------------------------------------------------ */
enum {                                   /* why a master was refused; travels in the detail */
    MW_NONE  = 0,
    MW_MEM   = 1,    /* out of memory                                                        */
    MW_ADDR  = 2,    /* an entry's own address is out of range or unreadable                 */
    MW_CAVE  = 3,    /* the space it writes a routine into is not empty - wrong build        */
    MW_HOOK  = 4,    /* a hook site does not hold code this master's cave re-executes        */
    MW_REACH = 5,    /* the routine reaches memory the game does not have (the scratch)      */
    MW_NOOFF = 6,    /* asked to remove it, and the file never said what was there           */
    MW_WRITE = 7,    /* a write failed after every check passed                              */
    MW_STATE = 8,    /* asked to remove it, and memory does not hold what it put there       */
    MW_PART  = 9     /* part of the master could not be read out of its own file              */
};

typedef struct cheat_master_plan {
    cheat_entry_t *es;
    int n;
    int abs_mode;
    unsigned char write[CHEAT_MAX_ENTRIES];
    intptr_t addr[CHEAT_MAX_ENTRIES];
    long long lo[CHEAT_MAX_ENTRIES], hi[CHEAT_MAX_ENTRIES];   /* the offsets each entry covers */
    int todo, skipped;
} cheat_master_plan_t;

static void cheat_master_release(cheat_master_plan_t *p) {
    if (!p) return;
    free(p->es);
    p->es = NULL;
    p->n = 0;
}

/* The cave entry a hook entry jumps into, or -1 when this entry is not a hook into our own caves.
   Every one of the 21 hooks in the 20 master blocks we ship satisfies this, so it is the structure
   the format actually has rather than a shape invented here. */
static int master_hook_cave(const cheat_master_plan_t *p, int i) {
    const cheat_entry_t *e = &p->es[i];
    if (e->on_len < 5 || (e->on[0] != 0xE9 && e->on[0] != 0xE8)) return -1;
    long long tgt = (long long)e->offset + 5 + rip_disp32(e->on + 1);
    for (int j = 0; j < p->n; j++) {
        if (j == i) continue;
        if (tgt >= p->lo[j] && tgt < p->hi[j]) return j;
    }
    return -1;
}

/* IS THIS CAVE STILL THE MASTER'S ROUTINE, with a cheat sitting in it?
 *
 * The cheats in these files are byte patches INSIDE the master's routine - CUSA05574_01.50 has eight
 * in one cave - so the moment one is on, the cave no longer equals the master's own bytes. An exact
 * memcmp therefore said "not in place", the cave was not all-zero either, and the second cheat in the
 * file was refused as a different build. Measured: 43 mod entries across the 20 master-bearing files
 * start inside a master span, 9 of those files have two or more sharing one.
 *
 * A byte may differ ONLY where some mod in this same document declares a run. Those positions are
 * exactly the ones a sibling cheat is allowed to own, and the file itself is what says so - the same
 * reasoning offset_known_state applies to mods, in the other direction.
 *
 * Returns 1 when every difference is accounted for. A wrong build fails it: its bytes differ
 * everywhere and almost none of those positions are declared by any mod. */
static int master_cave_is_ours(const char *json, const cheat_entry_t *me,
                               const unsigned char *cur, int wl) {
    if (wl <= 0 || wl > CHEAT_MAX_BYTES) return 0;
    unsigned char diff[CHEAT_MAX_BYTES];
    int ndiff = 0;
    for (int k = 0; k < wl; k++) {
        diff[k] = (unsigned char)(cur[k] != me->on[k]);
        if (diff[k]) ndiff++;
    }
    if (!ndiff) return 1;                         /* identical - the ordinary already-in-place case */
    long long lo = (long long)me->offset, hi = lo + wl;
    cheat_entry_t *es = (cheat_entry_t *)malloc(CHEAT_ENTS_BYTES);
    if (!es) return 0;
    const char *from = mods_array_start(json);
    for (int m = 0; from && m < CHEAT_MAX_MODS && ndiff > 0; m++) {
        const char *end = NULL;
        const char *blk = next_mod_block(from, &end);
        if (!blk) break;
        from = end;
        int n = parse_mod_entries_ex(blk, end, es, CHEAT_MAX_ENTRIES, NULL);
        for (int i = 0; i < n && ndiff > 0; i++) {
            int span = es[i].on_len > es[i].off_len ? es[i].on_len : es[i].off_len;
            long long a = (long long)es[i].offset;
            for (long long p = a; p < a + span; p++) {
                if (p < lo || p >= hi) continue;
                int k = (int)(p - lo);
                if (diff[k]) { diff[k] = 0; ndiff--; }
            }
        }
    }
    free(es);
    return ndiff == 0;
}

static int bytes_appear_in(const unsigned char *hay, int hn, const unsigned char *needle, int nn) {
    if (nn <= 0 || nn > hn) return 0;
    for (int i = 0; i + nn <= hn; i++) if (memcmp(hay + i, needle, (size_t)nn) == 0) return 1;
    return 0;
}

/* Decide the whole master. Returns the number of entries that WOULD be written (0 for "nothing to
   do", which includes "there is no master"), or -(one of MW_*) when it refuses. On any refusal, and
   on 0, the plan holds nothing to commit - but the caller must still release it, because the entry
   array is allocated as soon as a master exists (its offsets are what tell a mod whether it sits
   inside a cave, which the caller needs even when the master cannot be installed). */
static int cheat_master_decide(const char *json, pid_t pid, intptr_t base, int non_json,
                               int want_on, cheat_master_plan_t *p) {
    memset(p, 0, sizeof(*p));
    const char *mend = NULL;
    const char *mblk = cheat_master_span(json, &mend);
    if (!mblk) return 0;                                   /* no master: nothing to do, not an error */

    p->es = (cheat_entry_t *)malloc(CHEAT_ENTS_BYTES);
    if (!p->es) return -MW_MEM;
    /* A DROPPED ENTRY IS HALF A ROUTINE. parse_mod_entries_ex counts the entries it could not read
       (too long for the buffer, malformed hex), and this passed NULL - so a master missing a piece
       was installed as a routine with a hole in it. The mod path refuses exactly this, in those
       words; so does this now. */
    int mdropped = 0;
    p->n = parse_mod_entries_ex(mblk, mend, p->es, CHEAT_MAX_ENTRIES, &mdropped);
    if (p->n <= 0) { cheat_master_release(p); return 0; }
    if (mdropped > 0) return -MW_PART;
    p->abs_mode = non_json ? cheat_addr_mode(p->es, p->n, pid, base) : 0;

    for (int i = 0; i < p->n; i++) {
        int span = p->es[i].on_len > p->es[i].off_len ? p->es[i].on_len : p->es[i].off_len;
        p->lo[i] = (long long)p->es[i].offset;
        p->hi[i] = p->lo[i] + (span > 0 ? span : 1);
    }

    /* TAKING IT OUT NEEDS SOMETHING TO PUT BACK. The json form documents only the on bytes - two of
       Dark Souls II's four master entries replace real game code and the file never says what was
       there - so there is no honest revert and the game has to be closed. The Trainer <StartUP> form
       carries ValueOff, and for those this works. */
    if (!want_on) {
        for (int i = 0; i < p->n; i++) if (p->es[i].off_len <= 0) return -MW_NOOFF;
    }

    unsigned char cur[CHEAT_MAX_BYTES];
    for (int i = 0; i < p->n; i++) {
        const unsigned char *w = want_on ? p->es[i].on  : p->es[i].off;
        int wl                 = want_on ? p->es[i].on_len : p->es[i].off_len;
        const unsigned char *x = want_on ? p->es[i].off : p->es[i].on;   /* the documented opposite */
        int xl                 = want_on ? p->es[i].off_len : p->es[i].on_len;
        if (wl <= 0) { p->skipped++; continue; }
        intptr_t addr = cheat_entry_addr(&p->es[i], base, p->abs_mode);
        p->addr[i] = addr;
        if (!ADDR_OK(addr)) return -MW_ADDR;
        /* READ THE LONGER OF THE TWO RUNS, so both comparisons below are possible. Reading only wl
           meant a documented opposite state longer than what is being written could never be
           compared - which on the removal path (wl is off_len, xl is on_len) makes a master with a
           longer `on` permanently unremovable. */
        int rl = xl > wl ? xl : wl;
        if (rl > CHEAT_MAX_BYTES) rl = CHEAT_MAX_BYTES;
        if (mem_read(pid, addr, cur, (size_t)rl) != 0) {
            if (rl == wl || mem_read(pid, addr, cur, (size_t)wl) != 0) return -MW_ADDR;
            rl = wl;
        }
        if (memcmp(cur, w, (size_t)wl) == 0) { p->skipped++; continue; }   /* already in place */

        /* THE GATE THIS ENTRY GETS, and every entry gets one. Before this, an entry with no
           documented original state was written blind - so the master went into a running game on
           nothing but "these are not already the bytes I am about to write", which is not a check. */
        int ok = 0;
        if (xl > 0 && xl <= rl && memcmp(cur, x, (size_t)xl) == 0) {
            ok = 1;                              /* exactly the documented opposite state - as a mod */
        } else if (!want_on) {
            /* REMOVING. There is only one acceptable answer: memory holds what this file wrote. The
               cave and hook tests below are about installing and mean nothing here - "the space is
               empty" is false by definition when the thing you are removing is in it. */
            return -MW_STATE;
        } else {
            int cave = master_hook_cave(p, i);
            if (cave >= 0) {
                /* A HOOK. A trampoline re-executes the instruction it stole, so the bytes standing
                   here must appear inside the cave this jump goes to. If they do not, this is not
                   the site the file was written against. */
                ok = bytes_appear_in(p->es[cave].on, p->es[cave].on_len, cur, wl);
                if (!ok) return -MW_HOOK;
            } else {
                /* A CAVE. Empty is what the file describes - or the master's own routine with a
                   sibling cheat sitting in it, which is what this file is FOR. Both are "in place";
                   anything else is a different build. */
                ok = 1;
                for (int k = 0; k < wl; k++) if (cur[k]) { ok = 0; break; }
                if (!ok && master_cave_is_ours(json, &p->es[i], cur, wl)) {
                    p->skipped++;                  /* the routine is there; a cheat is in it */
                    continue;
                }
                if (!ok) return -MW_CAVE;
            }
        }
        if (!ok) return -MW_CAVE;
        intptr_t bad = 0;
        if (run_unreachable(w, wl, addr, pid, &bad)) return -MW_REACH;
        p->write[i] = 1;
        p->todo++;
    }
    return p->todo;
}

/* Write a plan that is already known to be complete.
 *
 * STOPS AT THE FIRST FAILURE, and *landed tells the caller how many went in before it. It used to
 * carry on writing and then report -MW_WRITE with no count, so a hook could be sitting over a cave
 * that never got filled while the owner was told "Nothing was changed". Every read and every gate has
 * already passed by the time this runs, so a failure here is the channel giving out - and continuing
 * past it is how you install half a routine.
 *
 * Returns entries written, or -MW_WRITE with *landed set. */
static int cheat_master_commit(cheat_master_plan_t *p, pid_t pid, int want_on, int *landed) {
    int written = 0;
    if (landed) *landed = 0;
    for (int i = 0; i < p->n; i++) {
        if (!p->write[i]) continue;
        const unsigned char *w = want_on ? p->es[i].on : p->es[i].off;
        int wl = want_on ? p->es[i].on_len : p->es[i].off_len;
        if (mem_write(pid, p->addr[i], w, (size_t)wl) != 0) {
            if (landed) *landed = written;
            return -MW_WRITE;
        }
        written++;
    }
    if (landed) *landed = written;
    return written;
}

/* WHAT MEMORY WILL LOOK LIKE ONCE THE MASTER'S PLAN HAS BEEN WRITTEN.
 *
 * A cave-resident cheat's documented original bytes are the MASTER'S bytes - God mode expects
 * 8B8370010000 at +0x2077807, which is the instruction the master's routine puts there. So a mod
 * gated against memory as it stands would be refused for every one of those cheats, and gating it
 * after writing the master is what let a refused mod leave an irreversible master behind. The gate
 * therefore reads memory and then overlays the master's PLAN before comparing.
 *
 * Driven by p->write[], not by the entry list: an entry the master is skipping is already in place,
 * so what was read is already right. */
static void master_overlay(const cheat_master_plan_t *p, intptr_t addr, unsigned char *buf, int len,
                           int want_on) {
    if (!p || p->n <= 0 || len <= 0) return;
    for (int i = 0; i < p->n; i++) {
        if (!p->write[i]) continue;
        /* WHAT THE PLAN WILL WRITE, WHICHEVER DIRECTION IT WAS DECIDED IN. This always overlaid `on`,
           which is right for an install and wrong for a removal - reachable only because the caller
           memsets the plan when turning a mod off, so it was a latent wrong answer rather than a live
           one. Saying it properly costs one argument. */
        const unsigned char *src = want_on ? p->es[i].on : p->es[i].off;
        int wl = want_on ? p->es[i].on_len : p->es[i].off_len;
        if (wl <= 0) continue;
        intptr_t lo = p->addr[i];
        for (int k = 0; k < len; k++) {
            intptr_t a = addr + k;
            if (a >= lo && a < lo + wl) buf[k] = src[a - lo];
        }
    }
}

/* Is this mod's own byte run inside one of the master's caves? Those cheats are patches to the
   master's routine and cannot work without it; anything else in the same file can. */
/* Does this mod's run OVERLAP one of the master's caves? A run that starts before a cave and reaches
   into it depends on the master just as much as one that starts inside it, and testing only the start
   offset missed that. Half-open spans on both sides, so touching ends do not count. */
static int master_covers(const cheat_master_plan_t *p, unsigned long long off, int len) {
    long long a = (long long)off, b = a + (len > 0 ? len : 1);
    for (int i = 0; i < p->n; i++)
        if (a < p->hi[i] && b > p->lo[i]) return 1;
    return 0;
}

/* The sentence for a refused master, for the log and for the detail. */
static const char *master_why_text(int why) {
    switch (why) {
    case MW_MEM:   return "out of memory";
    case MW_ADDR:  return "an address it needs is not in this game";
    case MW_CAVE:  return "the space it writes into is not empty - a different build";
    case MW_HOOK:  return "a hook site does not hold the code it expects - a different build";
    case MW_REACH: return "it needs memory below the game's image that this console does not map";
    case MW_NOOFF: return "the file does not say what was there before it";
    case MW_STATE: return "the game does not hold what this master wrote";
    case MW_PART:  return "part of it could not be read out of the cheat file";
    case MW_WRITE: return "a write failed";
    default:       return "";
    }
}

/* TURNING THE MASTER BACK OUT, for the Trainer form that documents its original bytes. Called by
   Disable-all once every mod has been reverted - the json form answers MW_NOOFF and is left alone. */
static int cheat_master_off(const char *json, pid_t pid, intptr_t base, int non_json,
                           char *why, size_t wsz) {
    if (why && wsz) why[0] = 0;
    cheat_master_plan_t p;
    int rc = cheat_master_decide(json, pid, base, non_json, 0, &p);
    int landed = 0;
    if (rc > 0) rc = cheat_master_commit(&p, pid, 0, &landed);
    if (why && wsz) {
        if (rc > 0) snprintf(why, wsz, "master removed: %d entr%s", rc, rc == 1 ? "y" : "ies");
        else if (rc == -MW_WRITE)
            snprintf(why, wsz, "master only PARTLY removed: %d entr%s put back, then a write failed - "
                     "close the game to clear the rest", landed, landed == 1 ? "y was" : "ies were");
        else if (rc < 0) snprintf(why, wsz, "master left in place - %s", master_why_text(-rc));
    }
    cheat_master_release(&p);
    return rc;
}

/* Does this document carry a master code, and can it be taken back out again?
 *
 * The answer to the second half is not a matter of opinion: a master can be reverted only if the
 * file says what was there before it. The json form (20 files) documents only `on` bytes for the two
 * places it patches, so there is nothing to put back and closing the game is the only way to clear
 * it. The Trainer <StartUP> form (73 files) carries ValueOff and can be undone. The panel says which
 * of the two an owner is dealing with, so "I turned everything off" means something definite. */
static int cheat_master_info(const char *json, int *removable) {
    if (removable) *removable = 0;
    const char *end = NULL;
    const char *blk = cheat_master_span(json, &end);
    if (!blk) return 0;
    cheat_entry_t *es = (cheat_entry_t *)malloc(CHEAT_ENTS_BYTES);
    if (!es) return 0;
    int n = parse_mod_entries_ex(blk, end, es, CHEAT_MAX_ENTRIES, NULL);
    int rem = n > 0;
    for (int i = 0; i < n; i++) if (es[i].off_len <= 0) { rem = 0; break; }
    free(es);
    if (removable) *removable = rem;
    return n > 0;
}

static int cheat_apply_blk(const char *json, int non_json, const char *blk, const char *end,
                           int index, int want_on, pid_t pid, intptr_t base, int force,
                           int check_only, char *detail, size_t dsz) {
    /* THE MASTER CODE IS DECIDED HERE AND WRITTEN AT THE END, with the mod, or not at all.
     *
     * It used to be installed right here, before the mod was gated - so a mod that was then refused
     * left an irreversible master behind in a running game. Worse, the master's own entries were
     * written blind, because a master documents no original bytes and there was nothing to gate on.
     * On a build the file does not fit that writes a jump into arbitrary code; on the build it DOES
     * fit it wrote Dark Souls II's cave, whose own first instruction addresses absolute 0x4000 - not
     * mapped - and the game died the instant a cheat was pressed. cheat_master_decide() now applies
     * the same standard to every master entry that a mod entry has always had, and run_unreachable()
     * is the gate that catches the second case. */
    cheat_master_plan_t mp;
    int master_n = 0, master_why = 0;
    if (want_on) {
        master_n = cheat_master_decide(json, pid, base, non_json, 1, &mp);
        if (master_n < 0) { master_why = -master_n; master_n = 0; }
    } else {
        memset(&mp, 0, sizeof(mp));
    }
    cheat_entry_t *ents = (cheat_entry_t *)malloc(CHEAT_ENTS_BYTES);
    if (!ents) { cheat_master_release(&mp); snprintf(detail, dsz, "out of memory"); return -1; }
    int dropped = 0;
    int n = parse_mod_entries_ex(blk, end, ents, CHEAT_MAX_ENTRIES, &dropped);
    if (n <= 0) {
        cheat_master_release(&mp);              /* ~1 MB, on the accept loop, every refused toggle */
        free(ents);
        snprintf(detail, dsz, "mod %d has no memory entries", index);
        return -3;
    }
    if (dropped > 0 && want_on && !force) {
        /* Part of this mod could not be read out of its file. Writing the rest installs half a
           hook - the classic way to hang a game - so the whole mod is refused (rc -4, its own
           toast). Turning OFF still runs: it only ever puts documented bytes back, each one gated
           below, so it can undo what a forced apply or an older build wrote and nothing more. */
        cheat_master_release(&mp);              /* and here - the other ~1 MB the audit found */
        free(ents);
        snprintf(detail, dsz, "mod %d has %d entr%s the engine cannot read (too large or malformed) "
                 "- refusing to apply part of it", index, dropped, dropped == 1 ? "y" : "ies");
        return -4;
    }
    int abs_mode = non_json ? cheat_addr_mode(ents, n, pid, base) : 0;

    /* ---- PASS ONE: DECIDE, WRITE NOTHING ---------------------------------------------------------
     *
     * A mod's entries are not independent edits. Measured on Dark Souls II's "1 hit kill": one entry
     * writes a 36-byte routine into a code cave (its documented "off" state is 36 zero bytes, which is
     * empty space, not original code) and the other patches a jump into that cave. Applying one
     * without the other is a jump into memory nobody wrote, and the game dies the next time it runs
     * that path - which is why it crashed on hitting an enemy rather than on switching the cheat on.
     *
     * This loop used to write as it went and `continue` past any entry that failed, calling the
     * result "written=1 failed=1". The file already refuses a mod it cannot fully PARSE for exactly
     * this reason ("writing the rest installs half a hook"); the same has to be true of an entry that
     * fails at write time. So every entry is decided here first, and one refusal cancels the mod.
     *
     * Costs nothing extra: the gate below already read every entry before writing it. */
    int written = 0, skipped = 0, failed = 0, displaced = -1;
    /* WHY an entry cannot be written, kept apart. All three used to be one `failed`, and the app
       then said "the cheat file was made for a different version" for all three - true only of
       f_bytes. f_addr is a cheat that points outside the process (a section/absolute entry), and
       f_read is the engine not being able to read at all, which on PS4 means the in-game helper
       has stopped. f_sec is how many of the failures carry a module index we do not place, which
       is the difference between "your game is the wrong version" and "this cheat is not for the
       main executable". */
    int f_addr = 0, f_read = 0, f_bytes = 0, f_sec = 0, f_write = 0, f_reach = 0;
    int written_plan = 0;             /* entries the plan would write - check_only reports this */

    /* DOES THIS MOD NEED THE MASTER? Only if its own bytes land inside one of the master's caves -
       those cheats are patches to the master's routine (five of Dark Souls II's seven are) and are
       meaningless without it. A mod elsewhere in the same file does not care, so a master that could
       not be installed must not take it down. */
    int needs_master = 0;
    for (int i = 0; i < n && mp.n > 0; i++) {
        int span = ents[i].on_len > ents[i].off_len ? ents[i].on_len : ents[i].off_len;
        if (master_covers(&mp, ents[i].offset, span)) { needs_master = 1; break; }
    }
    /* FORCE DOES NOT OVERRIDE THIS ONE. It exists to override the BYTE gate - "write it even though
       the bytes are not what the file says" - and writing a cheat into a routine that was never
       installed is not a gate being overridden, it is a jump into an empty cave. That is the crash
       this whole change exists to prevent, and force must not be a way back to it. */
    if (master_why && needs_master) {
        cheat_master_release(&mp);
        free(ents);
        snprintf(detail, dsz, "master_refused=%d entries=%d - refused, nothing was changed",
                 master_why, n);
        return -6;
    }

    unsigned char *plan = (unsigned char *)malloc((size_t)n);
    intptr_t *paddr = (intptr_t *)malloc((size_t)n * sizeof(intptr_t));
    if (!plan || !paddr) {
        cheat_master_release(&mp);
        free(plan); free(paddr); free(ents);
        snprintf(detail, dsz, "out of memory");
        return -1;
    }
    for (int i = 0; i < n; i++) {
        cheat_entry_t *e = &ents[i];
        const unsigned char *w = want_on ? e->on  : e->off;
        int wl                 = want_on ? e->on_len : e->off_len;
        const unsigned char *x = want_on ? e->off : e->on;      /* expected current state */
        int xl                 = want_on ? e->off_len : e->on_len;
        plan[i] = 0;
        paddr[i] = 0;
        if (wl <= 0) { skipped++; continue; }
        intptr_t addr = cheat_entry_addr(e, base, abs_mode);
        paddr[i] = addr;
        if (!ADDR_OK(addr)) { failed++; f_addr++; if (e->section) f_sec++; continue; }
        /* ANOTHER MODULE'S OFFSET IS NOT THIS MODULE'S. 306 entries in 103 shipped files carry a
           non-zero `section` - the index of a different loaded module in the tool that wrote them -
           and this engine resolves exactly one base. 305 of those addresses pass ADDR_OK, so without
           this the engine computes base + offset and writes there if the bytes happen to match. The
           refusal message has claimed since 3.82.0 that these are declined; now they are. Not force-
           able, because there is no version of "force" that makes an offset mean another module. */
        if (e->section) { failed++; f_sec++; continue; }
        if (!force) {
            /* EVERY write goes through the gate now. It used to run only when the on and off
               runs were the same length; 549 real entries (a 5-byte call replaced by a 2-byte
               jump, a longer patch over a short one) skipped it and were written blind into
               whatever build was running - and Disable-all put their "off" bytes back just as
               blind. Read the longer of the two runs so both comparisons are possible. */
            int rl = xl > wl ? xl : wl;
            unsigned char cur[CHEAT_MAX_BYTES];
            if (mem_read(pid, addr, cur, (size_t)rl) != 0) {
                /* the longer run may cross into a page the shorter one does not */
                if (rl == wl || mem_read(pid, addr, cur, (size_t)wl) != 0) {
                    failed++; f_read++; if (e->section) f_sec++; continue;
                }
                rl = wl;
            }
            /* AND WHAT THE MASTER IS ABOUT TO PUT THERE. Without this, every cheat that patches the
               master's own routine is refused, because the cave it lives in is still empty when this
               read happens - and writing the master first to avoid that is the bug this replaced. */
            master_overlay(&mp, addr, cur, rl, want_on);
            if (memcmp(cur, w, (size_t)wl) == 0) { skipped++; continue; }   /* already in state */
            if (xl > 0 && xl <= rl && memcmp(cur, x, (size_t)xl) == 0) {
                /* holds exactly the documented opposite state - the normal case */
            } else if (xl <= 0) {
                /* The file documents nothing to expect here (an "on"-only value write, one in the
                   whole JSON library). There is no state to gate on, so the write stands as it
                   always has; only the already-in-state test above applies. */
            } else {
                /* Not our expected state — but a sibling mod may legitimately own this hook.
                   Allow the hand-over when the bytes are a documented state; refuse otherwise. */
                int owner = -1;
                if (!offset_known_state(json, e->offset, cur, wl, &owner)) {
                    failed++; f_bytes++; if (e->section) f_sec++; continue;
                }
                if (owner >= 0 && owner != index) displaced = owner;
            }
        }
        /* AND WHAT THIS RUN ITSELF REACHES. A cheat that installs a routine brings its own code,
           and that code can address memory the game does not have: "1 hit kill" reads image base -
           0x3FC000 (absolute 0x4000), which is not inside any module this process has loaded. It
           applied cleanly and then killed the game the first time an enemy was hit. Five entries in
           the whole shipped library reach outside the image, all of them that same slot. */
        intptr_t bad_t = 0;
        if (run_unreachable(w, wl, addr, pid, &bad_t)) {
            failed++; f_reach++; continue;
        }
        plan[i] = 1;                    /* decided: this one is to be written */
        written_plan++;                 /* what a check-only run reports, and what pass two writes */
    }

    /* ---- THE REFUSAL, BEFORE ANYTHING IS TOUCHED ------------------------------------------------
     * One entry that cannot be written makes the whole mod unsafe, so nothing is written at all and
     * the game is left exactly as it was. `force` still goes ahead - that is what it is for - and the
     * detail says so, because a forced partial apply is a thing somebody should know they did. */
    if (failed > 0 && !force) {
        cheat_master_release(&mp);
        free(plan); free(paddr); free(ents);
        snprintf(detail, dsz,
                 "entries=%d ready=%d unwritable=%d bad_addr=%d unreadable=%d mismatch=%d "
                 "section=%d noreach=%d - refused, nothing was changed",
                 n, written + skipped, failed, f_addr, f_read, f_bytes, f_sec, f_reach);
        return -4;
    }

    /* ---- ASKED, NOT TOLD -------------------------------------------------------------------------
     * Everything above this line is reads and arithmetic - the same reads, the same gates and the same
     * reasons as a real apply. check_only stops here, so a caller can find out what would happen
     * without it happening. The number it returns is what the write loop below WOULD write, and the
     * detail is the same detail, so a diagnostic and the real thing cannot disagree. */
    if (check_only) {
        cheat_master_release(&mp);
        size_t cl = (size_t)snprintf(detail, dsz, "check=1 ");
        if (master_n > 0 && cl < dsz) cl += (size_t)snprintf(detail + cl, dsz - cl, "master=%d ", master_n);
        if (cl < dsz)
            snprintf(detail + cl, dsz - cl, "entries=%d would_write=%d skipped=%d dropped=%d",
                     n, written_plan, skipped, dropped);
        int would = written_plan;
        free(plan); free(paddr); free(ents);
        return would;
    }

    /* ---- THE MASTER GOES IN FIRST, now that the mod is known to be writable ----------------------
     * The order is the whole point: a cave must hold the master's routine before anything jumps into
     * it. Nothing above this line has touched the game. */
    if (master_n > 0) {
        int landed = 0;
        int mw = cheat_master_commit(&mp, pid, 1, &landed);
        if (mw < 0) {
            /* IT STOPPED AT THE FIRST FAILURE, so `landed` is how much of the routine is in the game.
               Saying "nothing was changed" here would be false, and it is the shape of failure this
               whole change exists to prevent - so it says what is there and what to do about it. */
            cheat_master_release(&mp);
            free(plan); free(paddr); free(ents);
            snprintf(detail, dsz,
                     "master_refused=%d master_landed=%d entries=%d - the master code stopped writing",
                     MW_WRITE, landed, n);
            return -6;
        }
        master_n = mw;
    }
    cheat_master_release(&mp);

    /* ---- PASS TWO: WRITE THE PLAN ---------------------------------------------------------------- */
    for (int i = 0; i < n; i++) {
        if (!plan[i]) continue;
        cheat_entry_t *e = &ents[i];
        const unsigned char *w = want_on ? e->on  : e->off;
        int wl                 = want_on ? e->on_len : e->off_len;
        if (mem_write(pid, paddr[i], w, (size_t)wl) == 0) written++; else { failed++; f_write++; }
    }
    free(plan);
    free(paddr);
    free(ents);
    /* master=N goes FIRST, so the app can say a master code went in as well. Everything after it
       writes at detail + dl and dl accumulates - the else arm below used to assign dl from
       snprintf(detail, ...) and would have written straight over the master prefix. */
    size_t dl = 0;
    if (master_n > 0 && dsz > 24)
        dl = (size_t)snprintf(detail, dsz, "master=%d ", master_n);
    if (displaced >= 0)
        dl += (size_t)snprintf(detail + dl, dsz - dl,
                               "entries=%d written=%d skipped=%d failed=%d displaced_mod=%d",
                               n, written, skipped, failed, displaced);
    else
        dl += (size_t)snprintf(detail + dl, dsz - dl,
                               "entries=%d written=%d skipped=%d failed=%d",
                               n, written, skipped, failed);
    if (dropped > 0 && dl < dsz)                      /* only reachable forced, or turning off */
        dl += (size_t)snprintf(detail + dl, dsz - dl, " dropped=%d", dropped);
    /* A WRITE THAT FAILED IN PASS TWO IS NOT A VERSION PROBLEM, and it used to be reported as one:
       f_write was counted and then dropped on the floor, so cheat_rc_message fell through to "the
       cheat file was made for a different version" while the master was already in the game. */
    if (f_write > 0 && dl < dsz)
        snprintf(detail + dl, dsz - dl, " writefail=%d", f_write);
    return failed ? -(100 + failed) : written;
}

/* WHAT TO SAY TO THE OWNER when the engine refuses. The routes below report rc and detail, and
   detail is for us: "entries=3 written=0 skipped=0 failed=3". errText() in the page only prettifies
   bare snake_case codes, so that line was being toasted at the owner exactly as written.

   Returns an empty string when there is nothing to explain, so a caller can simply omit the field.
   The numbers are read back out of detail rather than threaded through four call sites: this is our
   own format, produced a dozen lines above, and parsing it keeps the change to one function.

   PARTLY APPLIED IS ITS OWN ANSWER and matters more than it looks. cheat_apply_blk carries on past a
   failed entry, so rc can be negative while written is greater than zero - the mod has real bytes in
   the running game. Calling that a clean failure is how a tile gets repainted OFF over a game that
   has been half modified. */
static void cheat_rc_message(int rc, const char *detail, int want_on, char *out, size_t osz) {
    if (!osz) return;
    out[0] = 0;
    if (rc >= 0) return;
    int written = -1, entries = -1;
    const char *w = detail ? strstr(detail, "written=") : NULL;
    const char *e = detail ? strstr(detail, "entries=") : NULL;
    if (w) written = atoi(w + 8);
    if (e) entries = atoi(e + 8);
    if (written > 0) {
        snprintf(out, osz,
                 "Only part of this went into the game - %d of %d changes landed, so it is neither "
                 "fully on nor fully off. Turn it off, then on again. If it keeps happening the "
                 "cheat file was made for a different version of this game.",
                 written, entries > written ? entries : written);
    } else {
        /* SAY WHICH OF THEM HAPPENED. The detail carries each reason separately, and an absent key
           (an older payload, or a refusal from somewhere that does not count them) reads as 0 and
           falls through to the version sentence exactly as before. */
        int sec = detail && strstr(detail, "section=") ? atoi(strstr(detail, "section=") + 8) : 0;
        int bad = detail && strstr(detail, "bad_addr=") ? atoi(strstr(detail, "bad_addr=") + 9) : 0;
        int unr = detail && strstr(detail, "unreadable=") ? atoi(strstr(detail, "unreadable=") + 11) : 0;
        int nor = detail && strstr(detail, "noreach=") ? atoi(strstr(detail, "noreach=") + 8) : 0;
        int mre = detail && strstr(detail, "master_refused=")
                  ? atoi(strstr(detail, "master_refused=") + 15) : 0;
        int mland = detail && strstr(detail, "master_landed=")
                    ? atoi(strstr(detail, "master_landed=") + 14) : 0;
        int wfail = detail && strstr(detail, "writefail=")
                    ? atoi(strstr(detail, "writefail=") + 10) : 0;
        if (mre == MW_WRITE) {
            /* PART OF A ROUTINE IS IN THE GAME. Not "nothing was changed", which is what this used to
               say - and the one thing that matters is that closing the game is what clears it. */
            snprintf(out, osz,
                     "The engine stopped part-way through setting this game up: %d piece%s went in and "
                     "then the console stopped accepting writes. Close the game to clear it, then try "
                     "again.", mland, mland == 1 ? "" : "s");
        } else if (wfail > 0) {
            snprintf(out, osz,
                     "The console stopped accepting writes part-way through this cheat, so it is "
                     "neither on nor off. Close the game to clear it, then try again.");
        } else if (mre > 0) {
            /* This cheat is a patch INSIDE a master code's routine, and that routine could not be
               installed. The reason codes are MW_* in cheat_master_decide; the two an owner can act
               on are "a different build" and "memory this console does not have". */
            if (mre == MW_REACH)     /* the code, not a number that moves when the enum does */
                snprintf(out, osz,
                         "This cheat needs a scratch space in memory that this game does not have, so "
                         "nothing was changed. It was written for a setup this console cannot give it, "
                         "and forcing it would crash the game.");
            else
                snprintf(out, osz,
                         "This cheat is part of a master code, and the master code does not fit the "
                         "game you have installed. Nothing was changed. Try the cheats for your own "
                         "version from the version list.");
        } else if (nor > 0) {
            snprintf(out, osz,
                     "This cheat's own code reaches a place in memory that this game does not have, so "
                     "nothing was changed. That is a property of the cheat file, not of your game - "
                     "forcing it would crash the game.");
        } else if (sec > 0) {
            snprintf(out, osz,
                     "This cheat is written against a different part of the game than the one the "
                     "engine can change - it points into another piece of code the game loads, not "
                     "the main program. Nothing was changed. There is no way to use it as it is.");
        } else if (bad > 0) {
            snprintf(out, osz,
                     "This cheat points somewhere the game does not have memory, so nothing was "
                     "changed. The file is describing a different build of the game.");
        } else if (unr > 0) {
            snprintf(out, osz,
                     "The engine could not read the game's memory, so nothing was changed. Close "
                     "the game and open it again, then try once more.");
        } else {
            snprintf(out, osz,
                     "The game did not accept this, and nothing was changed. This almost always "
                     "means the cheat file was made for a different version of the game than the "
                     "one %s.",
                     want_on ? "installed" : "running");
        }
    }
}

/* Create each parent directory of a file path (mkdir per component). */
/* ===================== GAME PATCHES — implementation ======================== */

static int patch_file_for(const char *tid, char *out, size_t outsz) {
    if (!tid || !tid[0]) return -1;
    snprintf(out, outsz, "%s/%s.xml", CHEAT_PATCH_DIR, tid);
    struct stat st;
    if (stat(out, &st) == 0 && S_ISREG(st.st_mode)) return 0;
    out[0] = 0;
    return -1;
}

static int patch_count(const char *doc) {
    int n = 0;
    for (const char *p = doc; (p = strstr(p, "<Metadata")) != NULL; p += 9) n++;
    return n;
}

/* Nth <Metadata …> … </Metadata>. Returns the start of the opening tag. */
static const char *patch_block(const char *doc, int index, const char **end) {
    const char *p = doc;
    for (int i = 0; ; i++) {
        p = strstr(p, "<Metadata");
        if (!p) return NULL;
        if (i == index) {
            const char *e = strstr(p, "</Metadata>");
            *end = e ? e : (p + strlen(p));
            return p;
        }
        p += 9;
    }
}

/* Copy just the opening "<Metadata …>" tag.

   xml_attr() scans FORWARD without a bound, so looking an attribute up on the raw document made a
   patch inherit the NEXT patch's attributes. Caught on hardware: the first patch of a two-patch
   file picked up the second one's ImageBase, every one of its addresses then compared as
   out-of-range, and a perfectly good patch reported `lines=0`. Bound the search to the tag. */
static int patch_open_tag(const char *blk, char *out, size_t outsz) {
    const char *e = strchr(blk, '>');
    if (!e) { out[0] = 0; return -1; }
    size_t n = (size_t)(e - blk) + 1;
    if (n >= outsz) n = outsz - 1;
    memcpy(out, blk, n);
    out[n] = 0;
    return 0;
}

/* Decode the handful of XML entities that appear in Value="" text. */
static size_t patch_unescape(const char *in, size_t len, char *out, size_t outsz) {
    size_t o = 0;
    for (size_t i = 0; i < len && o + 1 < outsz; i++) {
        if (in[i] == '&') {
            if      (!strncmp(in + i, "&amp;",  5)) { out[o++] = '&';  i += 4; continue; }
            else if (!strncmp(in + i, "&lt;",   4)) { out[o++] = '<';  i += 3; continue; }
            else if (!strncmp(in + i, "&gt;",   4)) { out[o++] = '>';  i += 3; continue; }
            else if (!strncmp(in + i, "&quot;", 6)) { out[o++] = '"';  i += 5; continue; }
            else if (!strncmp(in + i, "&apos;", 6)) { out[o++] = '\''; i += 5; continue; }
        }
        out[o++] = in[i];
    }
    out[o] = 0;
    return o;
}

/* Encode one <Line Value="…"> into the raw bytes to write.

   These rules are read off the real library, not guessed:
     * "Resolution Patch (900p)" writes bytes32 0x00000640 and 0x00000384 — 1600 and 900 — and the
       720p variant writes 0x00000500 / 0x000002d0 — 1280 and 720. Written digit-for-digit those
       would be 1074003968, so the integer types carry a VALUE and go to memory little-endian,
       exactly as x86 stores an int.
     * "16:9 Aspect Ratio" writes bytes32 0x3fe38e39, which is IEEE-754 for 1.7777778.
     * float32 "0.016666667" is 1/60 — a 60 fps frame time — so: decimal -> IEEE-754, little-endian.
     * In CUSA00547 the utf8 strings sit exactly len+1 apart ("CROW" at …63, "DENGEKI" at …68), so
       the NUL terminator is written as well. Five of five cases agree.
   bytes/byte are raw byte sequences and are copied verbatim; a leading 0x is tolerated because the
   single-byte lines carry one ("Value=\"0x75\"") while the multi-byte ones do not. */
static int patch_encode_value(const char *type, size_t tlen, const char *val, size_t vlen,
                              unsigned char *out, size_t outsz) {
    char t[24];
    if (tlen >= sizeof(t)) return -1;
    memcpy(t, type, tlen); t[tlen] = 0;
    char v[PATCH_MAX_VAL * 2];
    vlen = patch_unescape(val, vlen, v, sizeof(v));

    if (!strcmp(t, "mask") || !strcmp(t, "mask_jump32")) return -2;   /* needs a signature scan */

    /* NO terminator. The reference writes exactly `s.size()` bytes — an appended NUL would
       clobber the byte after the string. (The library's utf8 lines do sit len+1 apart, but that
       is where the ORIGINAL data already had its terminator, not something the writer adds.) */
    if (!strcmp(t, "utf8")) {
        if (vlen > outsz) return -1;
        memcpy(out, v, vlen);
        return (int)vlen;
    }
    if (!strcmp(t, "utf16")) {
        if (vlen * 2 > outsz) return -1;
        for (size_t i = 0; i < vlen; i++) {   /* widen byte-by-byte, as the reference does */
            out[i * 2] = (unsigned char)v[i];
            out[i * 2 + 1] = 0;
        }
        return (int)(vlen * 2);
    }
    if (!strcmp(t, "float32")) {
        float f = strtof(v, NULL);
        if (outsz < 4) return -1;
        memcpy(out, &f, 4);                                 /* the PS5 is little-endian */
        return 4;
    }
    if (!strcmp(t, "float64")) {
        double d = strtod(v, NULL);
        if (outsz < 8) return -1;
        memcpy(out, &d, 8);
        return 8;
    }

    const char *h = v;
    int is_hex = (h[0] == '0' && (h[1] == 'x' || h[1] == 'X'));
    if (is_hex) h += 2;

    /* The scalar types. A 0x prefix means hex, anything else is decimal — never base-0, which
       would silently read a leading zero as octal. Every value in the current library carries
       the prefix, so this only matters for files we have not seen yet. */
    int width = 0;
    if      (!strcmp(t, "byte"))    width = 1;
    else if (!strcmp(t, "bytes16")) width = 2;
    else if (!strcmp(t, "bytes32")) width = 4;
    else if (!strcmp(t, "bytes64")) width = 8;
    if (width) {
        /* Hex is read unsigned, decimal signed — the reference uses stoull vs stoll, which is
           what lets a value like "-1" mean all-ones rather than failing. */
        unsigned long long u = is_hex ? strtoull(h, NULL, 16)
                                      : (unsigned long long)strtoll(h, NULL, 10);
        if ((size_t)width > outsz) return -1;
        for (int i = 0; i < width; i++) out[i] = (unsigned char)((u >> (8 * i)) & 0xFF);
        return width;
    }

    /* bytes — a raw hex sequence, written exactly as written. */
    int n = 0;
    for (const char *p = h; p[0] && p[1]; p += 2) {
        int hi = hexval(p[0]), lo = hexval(p[1]);
        if (hi < 0 || lo < 0) break;
        if ((size_t)n >= outsz) return -1;
        out[n++] = (unsigned char)((hi << 4) | lo);
    }
    return n > 0 ? n : -1;
}

static int patch_parse_lines(const char *blk, const char *end, patch_line_t *out, int max,
                             int *unsupported_out) {
    int n = 0, unsup = 0;
    unsigned long long imgbase = PATCH_NO_ASLR;   /* the default IS the base, not zero */
    size_t al = 0;
    char otag[2048];
    patch_open_tag(blk, otag, sizeof(otag));
    const char *ib = xml_attr(otag, "ImageBase", &al);
    if (ib && al) {
        char t[32];
        if (al < sizeof(t)) { memcpy(t, ib, al); t[al] = 0; imgbase = strtoull(t, NULL, 0); }
    }
    for (const char *p = blk; n < max; ) {
        p = strstr(p, "<Line");
        if (!p || p >= end) break;
        const char *lend = strchr(p, '>');
        if (!lend || lend > end) break;
        char tag[PATCH_MAX_VAL * 3];
        size_t tl = (size_t)(lend - p) + 1;
        if (tl >= sizeof(tag)) { p = lend + 1; continue; }
        memcpy(tag, p, tl); tag[tl] = 0;

        size_t tylen = 0, adlen = 0, vlen = 0;
        const char *ty = xml_attr(tag, "Type", &tylen);
        const char *ad = xml_attr(tag, "Address", &adlen);
        const char *va = xml_attr(tag, "Value", &vlen);
        if (!ty || !ad) { p = lend + 1; continue; }

        patch_line_t *L = &out[n];
        memset(L, 0, sizeof(*L));
        int enc = va ? patch_encode_value(ty, tylen, va, vlen, L->val, sizeof(L->val)) : -1;
        if (enc == -2) {
            /* mask / mask_jump32: Address is a byte SIGNATURE, not an address. Counted and
               reported rather than silently dropped, so a partly-applied patch cannot look whole. */
            L->unsupported = 1; unsup++; n++; p = lend + 1; continue;
        }
        if (enc <= 0) { p = lend + 1; continue; }

        char a[64];
        if (adlen >= sizeof(a)) { p = lend + 1; continue; }
        memcpy(a, ad, adlen); a[adlen] = 0;
        if (!(a[0] == '0' && (a[1] == 'x' || a[1] == 'X'))) { p = lend + 1; continue; }
        unsigned long long addr = strtoull(a, NULL, 0);
        if (addr < imgbase) { p = lend + 1; continue; }   /* would underflow — not a real line */
        addr -= imgbase;
        L->off = addr;
        L->len = enc;
        n++;
        p = lend + 1;
    }
    if (unsupported_out) *unsupported_out = unsup;
    return n;
}

/* Where the pre-patch bytes are kept so a patch can be undone. */
#define PATCH_UNDO_DIR CHEAT_PATCH_DIR "/.applied"

/* Keyed by the INSTALLED VERSION as well as title and index. A patch is an in-memory write that
   is gone the moment the game closes, so nobody presses Remove before taking a game update - and
   an update puts different code at the same offsets. Without the version in the name the next
   Apply on the new binary found the old binary's originals already saved, and Remove wrote them
   into code they never came from. An unknown version keys as "" (one file, as before). */
static void patch_undo_path(const char *tid, int index, const char *iver, char *out, size_t outsz) {
    char v[32] = {0};
    size_t k = 0;
    for (const char *p = iver ? iver : ""; *p && k < sizeof(v) - 1; p++) {
        char c = *p;
        int plain = (c >= '0' && c <= '9') || (c >= 'A' && c <= 'Z') || (c >= 'a' && c <= 'z') ||
                    c == '.' || c == '-';
        v[k++] = plain ? c : '_';       /* a version reads "01.03"; anything odd still names a file */
    }
    snprintf(out, outsz, "%s/%s_%d_%s.bin", PATCH_UNDO_DIR, tid, index, v);
}

/* The undo file, APPENDED to and never truncated. fopen(up, "wb") on every apply wiped it, so
   pressing Apply a second time (the button is enabled again after a refusal, and a fresh game
   launch makes every line writable again) destroyed the saved originals, and Remove then said
   "nothing to undo" over a patch that was very much in. Appending keeps every original ever
   recorded for this title+index+version: a relaunch appends the same bytes again and a revert
   writes them twice, which is harmless - for one installed version the original bytes at an
   offset never change. (A "first apply wins" O_EXCL file sat here briefly; it made the first
   apply's bytes permanent and never recorded a line that only a later apply reached.)
   patch_revert deletes the file after a complete revert. */
/* THE UNDO RECORD IS WRITTEN WITH A FILE DESCRIPTOR, NOT stdio.
 *
 * This engine is shared with the PS4 (tools/ps4_sync_cheat_core.py copies it into
 * ps4-app/onconsole/cheat_core.h) and fopen does not work in that payload - it returns NULL, and
 * every other writer in server_ps4.c uses open()/write() for that reason. With stdio here, a PS4
 * applied patches perfectly and then answered "no saved original bytes" to every Revert, for
 * ever, because the record it was supposed to read had never been written.
 *
 * O_APPEND so several lines of one patch accumulate, and so a second apply of the same patch
 * cannot overwrite the originals the first one saved. */
static int patch_undo_open(const char *up) {
    return open(up, O_WRONLY | O_CREAT | O_APPEND, 0777);
}

/* One line of the record: offset, length, then the bytes that were there.
 *
 * BUILT WHOLE AND WRITTEN ONCE. stdio hid the fact that this was three calls; with a raw
 * descriptor a short write between them leaves a record that reads back as garbage - and that
 * garbage is what a later Revert would write into a running game. Returns 0 only when the whole
 * record reached the file. */
static int patch_undo_write(int fd, unsigned long long off, int len, const unsigned char *was) {
    if (fd < 0 || len <= 0 || len > PATCH_MAX_VAL) return -1;
    unsigned char rec[sizeof(unsigned long long) + sizeof(int) + PATCH_MAX_VAL];
    size_t n = 0;
    memcpy(rec + n, &off, sizeof(off)); n += sizeof(off);
    memcpy(rec + n, &len, sizeof(len)); n += sizeof(len);
    memcpy(rec + n, was, (size_t)len);  n += (size_t)len;
    size_t done = 0;
    while (done < n) {
        ssize_t w = write(fd, rec + done, n - done);
        if (w <= 0) return -1;
        done += (size_t)w;
    }
    return 0;
}

/* Exactly n bytes, or a refusal. read() is allowed to return fewer than asked for on any file. */
static int undo_read_exact(int fd, void *buf, size_t n) {
    unsigned char *p = (unsigned char *)buf;
    size_t got = 0;
    while (got < n) {
        ssize_t r = read(fd, p + got, n - got);
        if (r <= 0) return -1;
        got += (size_t)r;
    }
    return 0;
}

/* Apply (or with dry=1 merely inspect) patch `index` of `tid` into the live process.

   Patches carry no "off" bytes, so the expect-gating the cheat engine relies on does not exist
   here — the real guard is AppVer, checked by the caller, plus three things done here:
     1. every address is range-checked before it is touched,
     2. the ORIGINAL bytes of every line actually written are saved first (patch_undo_open,
        appended to) - so the patch can be undone,
     3. every write is read back and compared, so a silent failure is reported as one.
   Returns the number of lines written, or a negative value on refusal - and a refusal it is
   whenever ANY line was refused, even if others landed: the count of what landed travels in
   `detail` so the toast can say "only partly applied" instead of "applied". */
static int patch_apply(const char *tid, int index, const char *iver, pid_t pid, intptr_t base,
                       int force, int dry, char *detail, size_t dsz) {
    (void)force;
    char file[600];
    if (patch_file_for(tid, file, sizeof(file)) != 0) return -1;
    long flen = 0;
    char *doc = slurp(file, &flen);
    if (!doc) return -2;
    const char *end = NULL;
    const char *blk = patch_block(doc, index, &end);
    if (!blk) { free(doc); return -3; }

    patch_line_t *lines = (patch_line_t *)malloc(PATCH_LINES_BYTES);
    if (!lines) { free(doc); return -4; }
    int unsup = 0;
    int n = patch_parse_lines(blk, end, lines, PATCH_MAX_LINES, &unsup);

    int written = 0, verified = 0, failed = 0, skipped = 0, already = 0;
    int undo = -1;
    int undo_tried = 0;      /* opened lazily, before the first write, so a run that writes
                                nothing leaves no empty undo file behind */
    for (int i = 0; i < n; i++) {
        patch_line_t *L = &lines[i];
        if (L->unsupported) { skipped++; continue; }
        intptr_t at = base + (intptr_t)L->off;
        if (!ADDR_OK(at)) { failed++; continue; }
        unsigned char cur[PATCH_MAX_VAL];
        if (mem_read(pid, at, cur, (size_t)L->len) != 0) { failed++; continue; }
        if (!memcmp(cur, L->val, (size_t)L->len)) { already++; continue; }   /* already patched */
        if (dry) { written++; continue; }
        if (!undo_tried) {
            undo_tried = 1;
            mkdir(PATCH_UNDO_DIR, 0777);
            char up[700];
            patch_undo_path(tid, index, iver, up, sizeof(up));
            undo = patch_undo_open(up);
        }
        if (undo >= 0 && patch_undo_write(undo, L->off, L->len, cur) != 0) {
            /* HALF AN UNDO RECORD IS WORSE THAN NONE: reverting it would restore some lines and
               then write wrong bytes at the next offset, into a running game. Abandon the record
               instead. Revert then says plainly that there is nothing saved. */
            close(undo);
            undo = -1;
        }
        if (mem_write(pid, at, L->val, (size_t)L->len) != 0) { failed++; continue; }
        written++;
        unsigned char back[PATCH_MAX_VAL];
        if (mem_read(pid, at, back, (size_t)L->len) == 0 && !memcmp(back, L->val, (size_t)L->len))
            verified++;
    }
    if (undo >= 0) close(undo);
    snprintf(detail, dsz, "lines=%d written=%d verified=%d already=%d failed=%d unsupported=%d",
             n - unsup, written, verified, already, failed, unsup);
    free(lines);
    free(doc);
    /* `failed && !written` here rounded a half-applied patch up to "applied" (rc > 0), and the
       "only partly applied" toast could never run because rc < 0 implied written == 0. */
    if (failed) return -100 - failed;
    return written;
}

/* Put back the bytes saved by patch_apply. */
static int patch_revert(const char *tid, int index, const char *iver, pid_t pid, intptr_t base,
                        char *detail, size_t dsz) {
    char up[700];
    patch_undo_path(tid, index, iver, up, sizeof(up));
    int f = open(up, O_RDONLY);
    if (f < 0) { snprintf(detail, dsz, "no saved original bytes"); return -1; }
    int restored = 0, failed = 0;
    for (;;) {
        unsigned long long off; int len;
        if (undo_read_exact(f, &off, sizeof(off)) != 0) break;
        if (undo_read_exact(f, &len, sizeof(len)) != 0) break;
        if (len <= 0 || len > PATCH_MAX_VAL) break;
        unsigned char buf[PATCH_MAX_VAL];
        if (undo_read_exact(f, buf, (size_t)len) != 0) break;   /* truncated: stop, restore what
                                                                   was whole, report the rest */
        intptr_t at = base + (intptr_t)off;
        if (ADDR_OK(at) && mem_write(pid, at, buf, (size_t)len) == 0) restored++;
        else failed++;
    }
    close(f);
    if (restored && !failed) unlink(up);
    snprintf(detail, dsz, "restored=%d failed=%d", restored, failed);
    return restored;
}

/* Read one attribute of patch `index` of `tid` (e.g. "Name", "AppVer"). Empty string if absent. */
static void patch_meta_attr(const char *tid, int index, const char *attr, char *out, size_t outsz) {
    out[0] = 0;
    char file[600];
    if (patch_file_for(tid, file, sizeof(file)) != 0) return;
    long fl = 0;
    char *doc = slurp(file, &fl);
    if (!doc) return;
    const char *end = NULL;
    const char *blk = patch_block(doc, index, &end);
    size_t l = 0;
    const char *p;
    char otag[2048];
    if (blk && patch_open_tag(blk, otag, sizeof(otag)) == 0 &&
        (p = xml_attr(otag, attr, &l)) != NULL && l < outsz) { memcpy(out, p, l); out[l] = 0; }
    free(doc);
}

/* The whole guarded apply/revert, shared by GET /api/patch/* and POST /api/mods/<tid>/apply.
   Writing it once means the safety gates cannot be present on one route and missing on the other. */
static void patch_action_json(const char *tid, int index, int force, int dry, int is_revert,
                              char *out, size_t outsz) {
    char file[600];
    if (patch_file_for(tid, file, sizeof(file)) != 0) {
        snprintf(out, outsz, "{\"ok\":false,\"error\":\"no_patch_file\",\"message\":\"There is no "
                             "game patch for this title.\"}");
        return;
    }
    /* A patch is compiled against exact code addresses, so it needs the live process. */
    char rtid[24] = {0};
    pid_t pid = 0; intptr_t base = 0;
    if (running_game(rtid, sizeof(rtid), &pid, &base) != 0 || strcmp(rtid, tid)) {
        snprintf(out, outsz, "{\"ok\":false,\"error\":\"game_not_running\",\"message\":\"Launch the "
                             "game first - a patch is written into its live memory.\"}");
        return;
    }
    if (!base) base = 0x400000;

    char iver[32] = {0}, pver[40] = {0};
    title_t *rows = (title_t *)calloc(MAX_TITLES, sizeof(title_t));
    int tn = rows ? read_console_titles(rows, MAX_TITLES) : -1;
    for (int i = 0; i < tn; i++)
        if (!strcmp(rows[i].tid, tid)) { snprintf(iver, sizeof(iver), "%s", rows[i].ver); break; }
    free(rows);
    patch_meta_attr(tid, index, "AppVer", pver, sizeof(pver));

    char detail[240] = {0};
    int rc;
    if (is_revert) {
        rc = patch_revert(tid, index, iver, pid, base, detail, sizeof(detail));
    } else {
        /* THE version gate. Dark Souls Remastered is exactly why it exists: the library holds a
           "Restore Debug Camera" patch built for 01.03 while 01.00 is installed, and those
           addresses land in unrelated code. Refused unless explicitly forced. Unlike a cheat,
           a patch has no documented "off" bytes to gate each write on, so this IS the guard. */
        if (iver[0] && pver[0] && strcmp(iver, pver) && !force) {
            snprintf(out, outsz,
                "{\"ok\":false,\"error\":\"version_mismatch\",\"app_ver\":\"%s\","
                "\"installed_version\":\"%s\",\"message\":\"This patch was built for v%s but v%s "
                "is installed. Its addresses point at different code, so nothing was written.\"}",
                pver, iver, pver, iver);
            return;
        }
        rc = patch_apply(tid, index, iver, pid, base, force, dry, detail, sizeof(detail));
    }

    char nm[200] = {0};
    patch_meta_attr(tid, index, "Name", nm, sizeof(nm));
    const char *what = nm[0] ? nm : "Patch";
    if (!dry) {
        patch_result_toast(what, is_revert, rc, detail, rtid);
    }
    /* The panel shows `message` over `detail` (errText in web/index.html). A refused apply used
       to hand it only the engineer-facing "lines=4 written=2 ..." string; say what it means.
       `partial` is the honest word for a patch that is half in. */
    int pwritten = 0;
    { const char *w = strstr(detail, "written="); if (w) pwritten = atoi(w + 8); }
    int partial = (!is_revert && rc <= -100 && pwritten > 0);
    const char *msg = "";
    if (!is_revert && rc <= -100)
        msg = partial ? "Only part of this patch was written before the rest was refused - it looks "
                        "built for another version. Remove it to undo the part that landed."
                      : "Its addresses do not match the game that is running - it is probably built "
                        "for another version. Nothing was written.";
    char ed[400], en[420];
    json_escape(detail, ed, sizeof(ed));
    json_escape(nm, en, sizeof(en));
    snprintf(out, outsz,
             "{\"ok\":%s,\"rc\":%d,\"title_id\":\"%s\",\"index\":%d,\"name\":\"%s\",\"pid\":%d,"
             "\"base\":\"0x%llx\",\"dry\":%s,\"app_ver\":\"%s\",\"installed_version\":\"%s\","
             "\"detail\":\"%s\",\"partial\":%s,\"message\":\"%s\"}",
             rc >= 0 ? "true" : "false", rc, tid, index, en, (int)pid,
             (unsigned long long)base, dry ? "true" : "false", pver, iver, ed,
             partial ? "true" : "false", msg);
}

/* JSON array of every patch in a title's file. `iver` (may be "") drives the compatible flag. */
static size_t patches_json(const char *tid, const char *iver, char *out, size_t outsz) {
    size_t len = 0;
    if (outsz < 3) { if (outsz) out[0] = 0; return 0; }   /* not even room for "[]" */
    out[0] = 0;
    char file[600];
    if (patch_file_for(tid, file, sizeof(file)) != 0) { snprintf(out, outsz, "[]"); return 2; }
    long flen = 0;
    char *doc = slurp(file, &flen);
    if (!doc) { snprintf(out, outsz, "[]"); return 2; }
    patch_line_t *lines = (patch_line_t *)malloc(PATCH_LINES_BYTES);
    out[len++] = '[';
    out[len] = 0;
    int total = patch_count(doc);
    int emitted = 0;
    for (int i = 0; i < total && i < PATCH_MAX_ITEMS; i++) {
        const char *end = NULL;
        const char *blk = patch_block(doc, i, &end);
        if (!blk) break;
        char nm[200] = {0}, note[400] = {0}, auth[120] = {0}, ver[40] = {0};
        size_t l = 0;
        const char *p;
        char otag[2048];
        patch_open_tag(blk, otag, sizeof(otag));
        if ((p = xml_attr(otag, "Name",   &l)) && l < sizeof(nm))   { memcpy(nm, p, l);   nm[l] = 0; }
        if ((p = xml_attr(otag, "Note",   &l)) && l < sizeof(note)) { memcpy(note, p, l); note[l] = 0; }
        if ((p = xml_attr(otag, "Author", &l)) && l < sizeof(auth)) { memcpy(auth, p, l); auth[l] = 0; }
        if ((p = xml_attr(otag, "AppVer", &l)) && l < sizeof(ver))  { memcpy(ver, p, l);  ver[l] = 0; }
        int unsup = 0, nl = 0;
        if (lines) nl = patch_parse_lines(blk, end, lines, PATCH_MAX_LINES, &unsup);
        /* An unknown installed version is not a mismatch — same rule the cheat side uses. */
        int compat = (!iver[0]) || (ver[0] && !strcmp(ver, iver));
        char en[420], eo[820], ea[260], ev[90];
        json_escape(nm, en, sizeof(en));
        json_escape(note, eo, sizeof(eo));
        json_escape(auth, ea, sizeof(ea));
        json_escape(ver, ev, sizeof(ev));
        /* `lines` is what we can actually WRITE. Counting the mask lines in it too made a
           patch of four unapplicable lines read as "lines=4, unsupported=4" — as though half
           of it would land. They are reported separately and the numbers now add up. */
        int applicable = nl - unsup;
        /* One entry is built on its own and copied in only if it fits WITH the closing bracket.
           The loop bound used to be `len < outsz - 700`, which wraps to SIZE_MAX for a buffer
           under 700 bytes (the /api/mods handler sizes this one from what its mods list left
           over), and a truncating snprintf still added its would-be length to len - so
           `outsz - len` wrapped too and the next entry wrote past the heap block. */
        char item[2400];
        int n = snprintf(item, sizeof(item),
            "%s{\"index\":%d,\"name\":\"%s\",\"description\":\"%s\",\"author\":\"%s\","
            "\"app_ver\":\"%s\",\"lines\":%d,\"unsupported\":%d,\"supported\":%s,"
            "\"compatible\":%s}",
            emitted ? "," : "", i, en, eo, ea, ev, applicable, unsup,
            (applicable > 0 && unsup == 0) ? "true" : "false",
            compat ? "true" : "false");
        if (n < 0 || (size_t)n >= sizeof(item)) break;
        if (len + (size_t)n + 2 > outsz) break;             /* entry + ']' + NUL must fit */
        memcpy(out + len, item, (size_t)n);
        len += (size_t)n;
        out[len] = 0;
        emitted++;
    }
    out[len++] = ']';
    out[len] = 0;
    free(lines);
    free(doc);
    return len;
}

static void mkparents(const char *path) {
    char tmp[512];
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p == '/') { *p = 0; mkdir(tmp, 0777); *p = '/'; }
    }
}

/* Self-extract the embedded web/ UI to WEB_ROOT so the ELF is fully self-contained: loading the updated
   ELF delivers the current UI to the PS5 with no separate push. Overwrites on every boot so updates apply. */
static void extract_web(void) {
    mkdir("/data", 0777);
    mkdir("/data/pkg-mutant-shop", 0777);
    mkdir(WEB_ROOT, 0777);
    for (int i = 0; i < WEB_FILES_COUNT; i++) {
        char full[512];
        snprintf(full, sizeof(full), "%s/%s", WEB_ROOT, WEB_FILES[i].path);
        mkparents(full);
        /* config.js is the ONE file here that is configuration rather than build output.
           `deploy.py app --companion http://PC:8710` writes the companion URL into it, and this
           loop then truncated it back to the shipped 348-byte comment on every single ELF load -
           so that documented flag has never survived a reload and window.PMS_API was always
           undefined. Ship it only when it is missing; never overwrite an existing one. */
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
            d += w; left -= (unsigned int)w;
        }
        close(fd);
    }
}

/* A LISTENING SOCKET FOR :PORT, with the options that matter, or -1.

   Factored out because the accept loop has to be able to build a SECOND one: a listener that has
   stopped accepting is not worth keeping, and the only way to replace it is to close it and bind
   again. The buffer sizes are not decoration - the receive window is negotiated during the
   handshake, so a replacement listener without them would quietly serve every later transfer at a
   fraction of the speed this console measured (78.6 MB/s against 108.7). */
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

int main(void) {
    /* A WRITE TO A CLOSED SOCKET MUST NOT KILL US. This build had no SIGPIPE handling at all
       while the PS4's has had SIG_IGN for some time, behind a measured klog line ("A user thread
       receives a fatal signal") - and this is the build that streams multi-gigabyte packages to
       the console's own downloader over /pkgfile/ and serves card art to a browser that abandons
       image loads as you scroll. The default action for SIGPIPE is to TERMINATE, and we are
       loaded into a shared process, so one client hanging up at the wrong moment takes the shop
       and everything beside it down. Ignore it and let write() return EPIPE, which every loop
       here already checks. */
    signal(SIGPIPE, SIG_IGN);
    jb_escalate_pid(getpid());     /* root so /user/appmeta (drwx------) is readable */
    /* AppInstUtil is NOT touched at boot: initialising it early (and registering packages
       before a real install) is what poisons the installer's IPMI state. It is loaded and
       initialised exactly once, lazily, on the first real install. */
    /* The dashboard tile is restored at boot, but ONLY when it is genuinely missing.
       The earlier version registered unconditionally on every load, which re-created dashboard
       entries and touched AppInstUtil before any real install — so it was removed altogether,
       and then a fresh console (which wipes /user, and with it /user/appmeta/PKGM00001) had no
       way to ever get the tile back. tile_is_registered() is the guard that makes it safe:
       normal boots do nothing at all, and it runs on the bootstrap thread so a slow
       AppInstUtil can never delay the server coming up. */
    extract_web();                 /* write the embedded UI to WEB_ROOT so loading this ELF updates the PS5 UI */
    cheat_library_bootstrap();     /* our own cheat library under /data/pkg-mutant-shop (background) */
    {   /* notice rest mode so we never keep using state that died while suspended */
        pthread_t rw;
        pthread_attr_t ra;
        pthread_attr_init(&ra);
        pthread_attr_setstacksize(&ra, 128 * 1024);
        if (pthread_create(&rw, &ra, rest_watchdog, NULL) == 0) pthread_detach(rw);
        pthread_attr_destroy(&ra);
    }
    ilog_trim_at_boot();
    ilog("==== BOOT: PKG MUTANT SHOP %s ====", SHOP_VERSION);
    {   /* OBSERVE ONLY: measure how much warning the console gives before it sleeps. Reads four
           named kernel event flags; stops nothing, writes nothing. See the block above. */
        pthread_t pw;
        pthread_attr_t pa;
        pthread_attr_init(&pa);
        pthread_attr_setstacksize(&pa, 128 * 1024);
        if (pthread_create(&pw, &pa, power_watchdog, NULL) == 0) pthread_detach(pw);
        pthread_attr_destroy(&pa);
    }

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) {
        notify("PKG MUTANT SHOP could not start\nThe console would not give it a network "
               "socket - load it again from Payload Manager");
        return 1;
    }
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
    {
        /* SET ON THE LISTENING SOCKET, WHICH IS THE ONLY PLACE IT FULLY COUNTS. An accepted socket
           inherits these, and the receive window is negotiated during the handshake - so setting
           them after accept() is already too late to widen the window for that connection.
           Measured 2026-10-02, 128 MB to /mnt/ext1, with the 1 MB copy buffer already in place:
           without this 78.6 MB/s, with it 108.7 MB/s. The console's own read path serves at 109.7,
           so this puts writing level with reading and both at the link.
           2 MB is deliberately modest: it is a receive window for a sub-millisecond LAN, not a
           buffer we have to fill, and the kernel only commits what a connection actually uses. */
        int bufsz = 2 << 20;
        setsockopt(srv, SOL_SOCKET, SO_RCVBUF, &bufsz, sizeof(bufsz));
        setsockopt(srv, SOL_SOCKET, SO_SNDBUF, &bufsz, sizeof(bufsz));
    }
    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(PORT);
    /* If an older build of the shop is still serving :PORT, ask it to stand down and take over.
       Reloading this ELF is the normal way the shop is updated, so a newer build must always win
       instead of silently exiting and leaving the old code running. */
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) {
        int taken = 0;
        for (int attempt = 0; attempt < 12 && !taken; attempt++) {
            if (attempt == 0) {
                int q = connect_local(PORT, 3000, 3000);
                if (q >= 0) {
                    const char *rq = "GET /api/quit HTTP/1.0\r\nHost: 127.0.0.1\r\nConnection: close\r\n\r\n";
                    write_all(q, rq, strlen(rq));
                    char tmp[64];
                    read(q, tmp, sizeof(tmp));
                    close(q);
                }
            }
            usleep(400000);
            close(srv);
            srv = socket(AF_INET, SOCK_STREAM, 0);
            if (srv < 0) break;
            setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
            if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) == 0) taken = 1;
        }
        if (!taken) {
            /* IS IT ACTUALLY SERVING, THOUGH? This said "already running - open it from
               Media" whenever the port was held, and a held port is not a working shop. The
               owner hit exactly that: a copy that was bound but answering nothing, told to go
               and open it, and the browser then showed a page that would not load. So ask it
               for its health before describing it - a wedged incumbent cannot answer, and
               that IS the answer. Synchronous: we exit immediately after, so a detached
               sender would never get to deliver this. */
            long hl = 0;
            char *hdoc = http_get_ip_to("127.0.0.1", PORT, "/api/health", &hl, 4);
            int alive = (hdoc && strstr(hdoc, "\"ok\""));
            free(hdoc);
            if (alive) {
                notify_sync("PKG MUTANT SHOP is already running\n"
                            "No need to load it again - open it from Media");
            } else {
                notify_sync("A copy of PKG MUTANT SHOP is stuck on this console\n"
                            "It still holds the port but has stopped answering. Restart "
                            "the console, then load the payload again");
            }
            return 0;
        }
    }
    listen(srv, 16);
    /* THE one startup toast. It fires the moment the server is listening, which is also the
       moment installs become possible: our engine is spawned per install and needs nothing running
       beforehand, so "the shop is up" and "you can install" are the same event. It therefore says
       so, and says where to go - a bare version number told the user nothing actionable. */
    notifyf("PKG MUTANT SHOP v%s is ready\nOpen it from Media, or %s:%d in any browser",
            SHOP_VERSION, lan_ip_str(), PORT);

    /* Bring up the bundled helpers in the background: the shop is already answering, so a slow
       payload launch cannot delay startup, and anything already running is left untouched. */
    {
        pthread_t pbt;
        pthread_attr_t pba;
        pthread_attr_init(&pba);
        /* Created ONCE, here, at boot - rest_watchdog never re-runs it. It still runs while the
           console is assembling itself after a cold boot or after rest mode (rest/prepare exits
           this process and Payload Manager reloads it, which is how "every wake" happens in
           practice) — the worst possible moment to overflow a default-sized stack. */
        pthread_attr_setstacksize(&pba, 256 * 1024);
        if (pthread_create(&pbt, &pba, payload_bootstrap, NULL) == 0) pthread_detach(pbt);
        pthread_attr_destroy(&pba);
    }

    g_started_ms = now_ms_local();     /* serving from here: what uptime_s in /api/health counts */
    long long accept_fails = 0;
    for (;;) {
        /* NO LISTENER MEANS MAKE ONE - see the failure branch below. A PS5 whose listening socket
           stops accepting is a console with no shop and no way back short of reloading the payload
           by hand, which the owner cannot do from the console itself: the shop is deliberately not
           in Payload Manager's autoload list. */
        if (srv < 0) {
            int fresh = pms_make_listener();
            if (fresh < 0) { usleep(250 * 1000); continue; }
            srv = fresh;
            accept_fails = 0;
            ilog("listening again on :%d", PORT);
        }
        int cl = accept(srv, 0, 0);
        if (cl < 0) {
            /* A BARE `continue` HERE PINS A CORE AND SAYS NOTHING. Descriptor exhaustion, or a
               listening socket the kernel will not hand connections from, becomes an invisible
               100% spin with :8710 still bound - which from outside looks exactly like the stall
               this release is about. Sleep a little so it cannot spin, and say so, rarely enough
               that the log stays readable. The PS4 has had all of this for some time. */
            accept_fails++;
            if (accept_fails == 1 || (accept_fails % 500) == 0)
                ilog("accept failed (%d) x%lld - the shop is still bound to :%d",
                     errno, accept_fails, PORT);
            /* AND THEN DO SOMETHING ABOUT IT. Logging a spin is not recovering from one. The old
               descriptor is closed BEFORE the replacement is bound, because SO_REUSEADDR will not
               hand out a port another socket is still listening on - the PS4 had a recovery that
               bound first and could therefore never run, through 119,000 measured failures.
               This build keeps no global for the listening descriptor: /api/quit hands the port
               over by _exit(0), so there is nothing else that could close it. */
            if ((accept_fails % 50) == 0) {
                int old = srv;
                srv = -1;
                close(old);
                ilog("the listening socket was not accepting - dropped it after %lld "
                     "failure(s), rebinding", accept_fails);
                continue;
            }
            usleep(20 * 1000);
            continue;
        }
        if (accept_fails) {
            ilog("accept recovered after %lld failure(s)", accept_fails);
            accept_fails = 0;
        }
        g_conns_served++;
        /* The accept loop is single-threaded, so a client that connects and then says NOTHING used
           to block this read() forever and take the WHOLE console API down with it - UI, install
           engine and cheat engine all stop answering until that socket closes. Measured: one idle
           TCP connection, zero bytes sent, and /api/health timed out for as long as it was held.
           A half-dead browser tab or a port scan was enough. A receive timeout bounds it: an
           unresponsive peer costs a few seconds, not the service. The long transfers do not run
           here - /pkgfile/ and the workers get their own threads and their own sockets. */
        {
            struct timeval rcvto;
            rcvto.tv_sec  = 8;
            rcvto.tv_usec = 0;
            setsockopt(cl, SOL_SOCKET, SO_RCVTIMEO, &rcvto, sizeof(rcvto));
            struct timeval sndto;
            sndto.tv_sec  = 30;                 /* a stalled reader must not pin the loop either */
            sndto.tv_usec = 0;
            setsockopt(cl, SOL_SOCKET, SO_SNDTIMEO, &sndto, sizeof(sndto));
        }
        char buf[8192];
        int n = read(cl, buf, sizeof(buf) - 1);
        if (n > 0) {
            buf[n] = 0;
            char path[1024] = "/";
            int is_post = !strncmp(buf, "POST ", 5);
            int is_get  = !strncmp(buf, "GET ", 4);
            int is_head = !strncmp(buf, "HEAD ", 5);   /* the daemon HEADs before it fetches */
            if (is_get || is_post || is_head) {
                const char *s = buf + ((is_post || is_head) ? 5 : 4);
                const char *e = strchr(s, ' ');
                if (e && (size_t)(e - s) < sizeof(path)) { memcpy(path, s, e - s); path[e - s] = 0; }
            }
            if (is_post && !strncmp(path, "/api/fs/write", 13) && request_origin_ok(buf) &&
                sec_fetch_ok(buf)) {
                /* Before the generic POST path, which caps a body at 8 KB. Handed to its own
                   thread with the socket, like /pkgfile/: a multi-GB upload used to run right
                   here and freeze every other request until it finished. If the worker cannot be
                   started it is served inline, exactly as before. */
                char *he = strstr(buf, "\r\n\r\n");
                int hl = he ? (int)(he - buf) + 4 : n;
                if (fs_write_serve(cl, path, buf, n, hl) == 0) continue;   /* worker owns cl now */
                fs_recv_write(cl, path, buf, hl, he ? n - hl : 0);
                close(cl);
                continue;
            }
            if (!request_origin_ok(buf)) {
                /* A page with a public hostname is driving us. Nothing here is for it. */
                send_status(cl, "403 Forbidden", "text/plain",
                            "this server only answers pages served from a private address");
                close(cl);
                continue;
            }
            if (route_changes_state(path, is_post) && !sec_fetch_ok(buf)) {
                /* A browser loaded a state-changing route as an image, a script or a navigation -
                   with no Origin and no Referer, which is how a hostile page slips past the guard
                   above. The UI never does this; it calls everything with fetch(). */
                send_status(cl, "403 Forbidden", "text/plain",
                            "this route is not a page or an image");
                close(cl);
                continue;
            }
            if ((is_get || is_head) && !strncmp(path, "/pkgfile/", 9)) {
                /* Package streaming for the install daemon. Handed to its own thread and
                   the socket goes with it: a multi-GB transfer must not block the accept
                   loop, or the UI and the cheat engine freeze for the whole install. */
                if (pkgfile_serve(cl, path, buf) == 0) continue;   /* worker owns cl now */
            } else if (is_get) {
                handle(cl, path, buf);   /* handle() strips the ?query itself (register-pc needs it) */
            } else if (is_post) {
                /* Read the whole body before answering: a short read here is what
                   made an earlier version of the PC side hang until the client
                   timed out. Content-Length is authoritative. */
                /* WHERE THE BODY STARTS IS NOT WHERE THE BUFFER STARTS. `body` points into buf,
                   past the headers, and the guard here used to compare Content-Length against the
                   size of the WHOLE buffer - it never subtracted the offset. With ordinary browser
                   headers (300-odd bytes) a POST declaring a length just under 8191 therefore wrote
                   past the end of this 8 KB stack array, and any machine on the LAN could send one.
                   The PS4 already carries this fix; this is the same shape, including the heap
                   escape - refusing with 413 instead would break the legitimate long-path requests
                   /api/install sends, which is a new bug in place of an old one. */
                char *hdr_end = strstr(buf, "\r\n\r\n");
                int blen = 0;
                const char *cl_h = strcasestr_local(buf, "content-length:");
                if (cl_h) blen = atoi(cl_h + 15);
                if (blen < 0) blen = 0;
                char *body = hdr_end ? hdr_end + 4 : NULL;
                char *heap = NULL;
                if (body) {
                    int have = n - (int)(body - buf);
                    if (have < 0) have = 0;
                    int room = (int)sizeof(buf) - 1 - (int)(body - buf);
                    if (room < 0) room = 0;
                    if (blen > POST_BODY_MAX) {
                        send_status(cl, "413 Payload Too Large", "application/json",
                                    "{\"ok\":false,\"error\":\"that request is too large for this console\"}");
                        close(cl);
                        continue;
                    }
                    if (blen > room) {
                        heap = (char *)malloc((size_t)blen + 1);
                        if (!heap) {
                            send_status(cl, "503 Service Unavailable", "application/json",
                                        "{\"ok\":false,\"error\":\"the console is out of memory right now\"}");
                            close(cl);
                            continue;
                        }
                        if (have > 0) memcpy(heap, body, (size_t)have);
                        body = heap;
                    }
                    while (have < blen) {
                        int r = (int)read(cl, body + have, (size_t)(blen - have));
                        if (r <= 0) break;
                        have += r;
                    }
                    body[have] = 0;
                }
                handle_post(cl, path, body ? body : "");
                free(heap);
            } else {
                send_status(cl, "405 Method Not Allowed", "text/plain", "GET or POST only");
            }
        }
        close(cl);
    }
    return 0;
}

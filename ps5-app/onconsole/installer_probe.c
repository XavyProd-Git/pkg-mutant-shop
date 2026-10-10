/*
 * pms-installer.elf — OUR install engine.
 *
 * WHY IT IS A SEPARATE PROCESS
 * ----------------------------
 * Measured on 2026-08-25, same package, same URI string, same console, the same second:
 *
 *     our in-process call   sceAppInstUtilInstallByPackage(...)  ->  0x80B2116F
 *     etaHEN's call         the identical call, identical bytes  ->  SUCCESS
 *
 * Identical arguments, different result — so the difference is a property of the calling PROCESS.
 * We then matched etaHEN's process credentials field by field (authid, uid/ruid/svuid/rgid/svgid,
 * jaildir, the capability mask, the attribute block) and it STILL returned 0x80B2116F. The ucred
 * is not it either.
 *
 * What is left is how the process was created. etaHEN's installer is not code injected into
 * somebody else's process — `elfldr_spawn("/", STDOUT_FILENO, util_elf, "etaHEN Utility Daemon")`
 * (commands.cpp:591) makes it a fresh process of its own. Our shop ELF IS injected into a hijacked
 * host. An APP install has to resolve an application slot; an add-on does not, which is exactly the
 * split we saw.
 *
 * So the install call lives here, in a process Payload Manager spawns per install. Proven:
 * Castle Crashers (CUSA14409) and Riptide GP2 (CUSA02365), both absent beforehand, reached bgft
 * status 1036 with title='PKG MUTANT SHOP'.
 *
 * WHY IT IS SO SMALL
 * ------------------
 * It does one thing and exits. There is no daemon to wedge, no port to unstick and no scratch state
 * to clear between installs — which is exactly why the third-party host needed a "cleartmp" step
 * between queued jobs and we do not.
 *
 * PROTOCOL — files, deliberately: no port, no parser, nothing to go wrong.
 *   reads   /data/pkg-mutant-shop/installer-req.txt
 *             line 1  the URI (http:// or a local path)
 *             line 2  OPTIONAL - a display name, used in the on-screen toast
 *             line 3  OPTIONAL - the shop's per-request token, echoed back unchanged
 *   writes  /data/pkg-mutant-shop/installer-res.json
 *             {"ok":…,"rc":"0x…","init_rc":"0x…","content_id":"…","pid":…,"uri":"…","token":"…"}
 *
 * The token is how the shop tells THIS run's verdict from a late one: an installer still
 * pre-allocating a large package when the shop's lane gave up used to write its result after the
 * next request had started, and that result was read as the new job's. The shop ignores a verdict
 * whose token is not the one it wrote. The request file is consumed once it has been read, so an
 * installer launched by hand afterwards finds nothing and installs nothing.
 */
#include <fcntl.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include <sys/stat.h>

#include <ps5/kernel.h>

#include "jb.h"

#define REQ_PATH "/data/pkg-mutant-shop/installer-req.txt"
#define RES_PATH "/data/pkg-mutant-shop/installer-res.json"

/* THE ICON, NOT THE LOGO. web/assets/icon0.png is the square 512x512 mark; web/assets/logo.png is
   the wide wordmark and would be cropped to nothing in a square notification slot. The shop ELF
   extracts web/ to this path on every boot, so it is always present and always matches the running
   build. sceKernelSendNotificationRequest takes an icon URI - that is how the toast carries our
   artwork instead of the generic system glyph. */
#define PMS_ICON "/data/pkg-mutant-shop/web/assets/icon0.png"

/* Byte-identical to etaHEN's (Source Code/util/include/common_utils.h:54-88 — checked against the
   real header, not assumed). MetaInfo 48 B, SceAppInstallPkgInfo 56 B, PlayGoInfo 9984 B. */
typedef struct { char content_id[0x30]; int content_type; int content_platform; } SceAppInstallPkgInfo;
typedef struct {
    const char *uri, *ex_uri, *playgo_scenario_id, *content_id, *content_name, *icon_url;
} MetaInfo;
typedef struct {
    char languages[30][8];
    char playgo_scenario_ids[64][3];
    char content_ids[64][0x30];
    unsigned char unknown[6480];
} PlayGoInfo;

extern int sceAppInstUtilInitialize(void);
extern int sceAppInstUtilInstallByPackage(MetaInfo *, SceAppInstallPkgInfo *, PlayGoInfo *);
extern int sceUserServiceInitialize(void *);
extern int sceKernelSendNotificationRequest(int, void *, size_t, int);

/* IDENTICAL to notify_request_t in server.c, and to etaHEN's OrbisNotificationRequest. This is a
   standalone ELF so it cannot share the type - if one changes, change both.

   THE OLD LAYOUT HERE HAD NEVER RENDERED A SINGLE TOAST. It began `pad[0x10]` and put the icon
   flag at 16 and the message at 17 - twenty-eight bytes ahead of the real record. The byte the
   shell reads as the icon flag (44) was therefore message[27], and every message this binary
   sends is longer than 27 characters, so the flag was ALWAYS set, the access(PMS_ICON) gate below
   was bypassed, and the length passed was 3089 instead of 3120. This is the spawned installer:
   it emits the toast for every PC-driven install. None of them ever drew. */
typedef struct {
    int32_t type, req_id, priority, msg_id, target_id, user_id;
    int32_t unk1, unk2, app_id, error_num, unk3;   /* 0x00 .. 0x2B */
    char    use_icon_image_uri;                    /* 0x2C */
    char    message[1024];                         /* 0x2D */
    char    uri[1024];                             /* 0x42D */
    char    unkstr[1024];                          /* 0x82D */
} notify_request_t;                                /* 0xC30 */
_Static_assert(sizeof(notify_request_t) == 0xC30,
               "the shell only draws a 0xC30 record - a padding change here silences every toast");

static void notifyf(const char *fmt, ...) {
    notify_request_t req;
    va_list ap;
    memset(&req, 0, sizeof(req));
    va_start(ap, fmt);
    vsnprintf(req.message, sizeof(req.message), fmt, ap);
    va_end(ap);
    /* Plain form only. Branding is being retried deliberately behind GET /api/notify?icon=N on the
       shop, where the result can be judged by LOOKING AT THE TELEVISION; until that says otherwise
       this binary sends the form that is known to draw. */
    /* Sent on THIS thread: main() is about to return, and a detached notifier thread would be
       killed before it could deliver. */
    sceKernelSendNotificationRequest(0, &req, sizeof(req), 0);
}

static int read_request(const char *path, char *uri, size_t urisz, char *label, size_t labelsz,
                        char *token, size_t toksz) {
    int fd = open(path, O_RDONLY);
    if (fd < 0) return -1;
    char buf[2048] = {0};
    ssize_t n = read(fd, buf, sizeof(buf) - 1);
    close(fd);
    if (n <= 0) return -1;
    buf[n] = 0;

    char *nl = strchr(buf, '\n');
    if (nl) {
        *nl = 0;
        size_t j = 0;
        const char *p = nl + 1;
        for (; *p && *p != '\r' && *p != '\n' && j < labelsz - 1; p++)
            label[j++] = *p;
        label[j] = 0;
        /* Line 3, the token: digits only, so nothing else in the file can ever reach the JSON. */
        while (*p && *p != '\n') p++;
        if (*p == '\n') {
            size_t k = 0;
            for (p++; *p >= '0' && *p <= '9' && k < toksz - 1; p++) token[k++] = *p;
            token[k] = 0;
        }
    }
    for (char *p = buf; *p; p++)
        if (*p == '\r') { *p = 0; break; }
    snprintf(uri, urisz, "%s", buf);
    return uri[0] ? 0 : -1;
}

static void write_result(const char *json) {
    int fd = open(RES_PATH, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (fd < 0) return;
    ssize_t w = write(fd, json, strlen(json));
    (void)w;
    close(fd);
}

/* IDENTICAL BY DESIGN to install_error_text() in server.c - this is a standalone ELF and cannot
   share the function, so if one changes, change both. Everything this project has actually seen
   come back from sceAppInstUtilInstallByPackage on hardware:

     0x80B2116F  SCE_PLAYGO_ERROR_CORE_INVALID_SLOT - refused before a download task exists. It is
                 what an INJECTED payload gets; a spawned one (this) does not, so seeing it here
                 means something went wrong with the spawn itself.
     0x80B22404  the installer's own 404 - it fetched the URL and got nothing.
     0x80F00003  SCE_NP_DRM_CONTENT_ERROR_UNSUPPORTED - not a PKG at all.
     0x80A4xxxx  the console's app database, NOT the package.
     0x80B2xxxx  anything else from the installer.
   Unknown codes still produce a true sentence, so this never has to be exhaustive. */
static const char *install_error_text(unsigned rc) {
    switch (rc) {
    case 0x80B2116Fu:
        return "The console turned the request down before it started - reload the shop from "
               "Payload Manager and try again";
    case 0x80B22404u:
        return "The console could not fetch the file - check the PC sharing it is still awake";
    case 0x80B21104u:
        /* Kept in step with server.c and companion/server.py - all three carry this table. No bgft
           row is created at all, and the generic 0x80B2xxxx bucket below would tell the user to
           re-download, which cannot help. */
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
    return "The console refused it - nothing was installed";
}

int main(void) {
    jb_escalate_pid(getpid());
    sceUserServiceInitialize(0);

    char uri[1200] = {0}, label[240] = {0}, token[32] = {0};
    if (read_request(REQ_PATH, uri, sizeof(uri), label, sizeof(label), token, sizeof(token)) != 0) {
        write_result("{\"ok\":false,\"error\":\"no request file\"}");
        /* Reached only if this ELF was launched by hand from Payload Manager instead of by
           the shop, so say that rather than describing an internal hand-off. */
        notifyf("Nothing to install\nStart installs from PKG MUTANT SHOP, not from Payload Manager");
        return 1;
    }
    /* Consume the request the moment it has been read. It used to stay on disk until the
       companion's cleanup, so a copy of this installer launched by hand re-ran the LAST install
       on top of whatever was live by then - the duplicate-install shape that has already taken
       this console down. The shop removes it too; this is the belt to that brace. */
    unlink(REQ_PATH);

    int init_rc = sceAppInstUtilInitialize();

    PlayGoInfo playgo;
    SceAppInstallPkgInfo pkg;
    memset(&playgo, 0, sizeof(playgo));
    memset(&pkg, 0, sizeof(pkg));

    MetaInfo meta;
    memset(&meta, 0, sizeof(meta));
    meta.uri = uri;
    meta.ex_uri = "";
    meta.playgo_scenario_id = "";
    meta.content_id = "";
    /* content_name is what the CONSOLE files the job under, so a real game name here means the
       system's own Downloads entry reads properly instead of showing our app's name. */
    meta.content_name = label[0] ? label : "PKG MUTANT SHOP";
    meta.icon_url = "";

    int rc = sceAppInstUtilInstallByPackage(&meta, &pkg, &playgo);

    char cid[64] = {0};
    snprintf(cid, sizeof(cid), "%.48s", pkg.content_id);

    char out[1900];
    snprintf(out, sizeof(out),
             "{\"ok\":%s,\"rc\":\"0x%08X\",\"init_rc\":\"0x%08X\",\"via\":\"spawned-process\","
             "\"pid\":%d,\"authid\":\"0x%016llx\",\"content_id\":\"%s\",\"uri\":\"%s\","
             "\"token\":\"%s\"}",
             rc == 0 ? "true" : "false", (unsigned)rc, (unsigned)init_rc, (int)getpid(),
             (unsigned long long)kernel_get_ucred_authid(getpid()), cid, uri, token);
    write_result(out);

    /* What the user sees on the TV. The console posts its own "Ready to play" toast when the
       download finishes; ours says what is happening RIGHT NOW — the console accepted the job and
       has started fetching. Saying "installed" here would be the same lie this project has
       removed everywhere else. */
    if (rc == 0)
        notifyf("%s is installing\nThe console is downloading it now - watch your home screen",
                label[0] ? label : "Your game");
    else
        /* The code goes to installer-res.json and from there into install.log and the app's queue
           row, all of which keep it; the screen gets the sentence alone. A hex number on a
           television is the one thing the house style forbids, and it used to ride along here. */
        notifyf("%s could not be installed\n%s", label[0] ? label : "That package",
                install_error_text((unsigned)rc));
    return rc == 0 ? 0 : 1;
}

/*
 * PKG MUTANT SHOP - unified PS5 payload  (ps5-payload-dev SDK)
 * ============================================================================
 * ONE elf that does everything:
 *   1. escalates privileges (jb.c, CheatRunner's method)
 *   2. installs / refreshes the dashboard TILE  — IF the tile .pkg is present on
 *      the console at /data/pldmgr/pms-tile.pkg (deploy it once; harmless to skip)
 *      via the system installer: LoadStartModule libSceAppInstUtil -> AppInstallPkg
 *   3. shows an on-screen notification and opens the shop in the PS5 browser
 *   4. keeps listening on TCP :9099 for one-line commands from the companion:
 *        notify <text>   -> PS5 on-screen notification
 *        open <url>      -> open the shop (defaults to PMS_URL)
 *        installtile     -> re-run the tile install on demand
 *        ping            -> health check
 *
 * Build (WSL, ps5-payload-dev SDK):  bash build-wsl.sh  [PMS_URL]
 */
#include <stdio.h>
#include <string.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdlib.h>
#include <unistd.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <netinet/in.h>

#include <ps5/kernel.h>
#include "jb.h"

/* ---- real PS5 system calls (SDK samples: notify, browser, module load) ---- */
#define SCE_NOTIFICATION_LOCAL_USER_ID_SYSTEM 0xFE
int sceNotificationSend(int userId, bool isLogged, const char *payload);
int sceSystemServiceLaunchWebBrowser(const char *uri, void *unused);
int sceUserServiceInitialize(void *unused);   /* required before LaunchWebBrowser */
int sceKernelLoadStartModule(const char *name, size_t argc, const void *argv,
                             uint32_t flags, void *opt, int *pRes);

/* companion sets this at build time: -DPMS_URL="http://<pc-ip>:8710" */
#ifndef PMS_URL
#define PMS_URL "http://10.0.0.76:8710"
#endif
#define CTRL_PORT 9099

/* Tile install (folded in from tile-appinst/tile-appinstall.c). The install service
 * sees FTP's /data/... as /user/data/..., so we check one path and install the other. */
#define APPINST_SPRX     "/system/common/lib/libSceAppInstUtil.sprx"
#define TILE_PKG_DISK    "/data/pldmgr/pms-tile.pkg"        /* where we look for it   */
#define TILE_PKG_INSTALL "/user/data/pldmgr/pms-tile.pkg"   /* what the installer opens */
/* verified Sony NIDs for libSceAppInstUtil on 12.70 */
#define NID_APPINST_INIT    "540lotO7oHE"
#define NID_APPINST_TERM    "kLLazhNh6d4"
#define NID_APPINST_PKG     "bpLyMf0oVwQ"   /* sceAppInstUtilAppInstallPkg */

#ifndef PMS_MSG_OPENING
#define PMS_MSG_OPENING "Opening PKG MUTANT SHOP..."
#endif
#ifndef PMS_AUTOOPEN
#define PMS_AUTOOPEN 1
#endif

static const char *TOAST_FMT =
  "{\"rawData\":{\"viewTemplateType\":\"InteractiveToastTemplateB\",\"channelType\":\"Downloads\","
  "\"useCaseId\":\"IDC\",\"toastOverwriteType\":\"No\",\"isImmediate\":true,\"priority\":100,"
  "\"viewData\":{\"icon\":{\"type\":\"Predefined\",\"parameters\":{\"icon\":\"download\"}},"
  "\"message\":{\"body\":\"%s\"},\"subMessage\":{\"body\":\"PKG MUTANT SHOP\"}},"
  "\"platformViews\":{\"previewDisabled\":{\"viewData\":"
  "{\"icon\":{\"type\":\"Predefined\",\"parameters\":{\"icon\":\"download\"}},"
  "\"message\":{\"body\":\"%s\"}}}}},\"localNotificationId\":\"7710\"}";

static void notify(const char *msg) {
    char json[2600], safe[1024];
    size_t j = 0;
    for (size_t i = 0; msg[i] && j < sizeof(safe) - 1; i++) {
        char ch = msg[i];
        if (ch == '"' || ch == '\\') ch = '\'';
        safe[j++] = ch;
    }
    safe[j] = 0;
    snprintf(json, sizeof(json), TOAST_FMT, safe, safe);
    sceNotificationSend(SCE_NOTIFICATION_LOCAL_USER_ID_SYSTEM, true, json);
}

static void open_shop(const char *url) {
    int rc = sceSystemServiceLaunchWebBrowser(url ? url : PMS_URL, 0);
    if (rc) {
        char b[128];
        snprintf(b, sizeof(b), "Browser launch failed: 0x%x (12.70 may block it)", (unsigned)rc);
        notify(b);
    }
}

/* Install / refresh the dashboard tile via the system installer. No-op (returns 0)
 * if the .pkg isn't on the console — so a plain "open the shop" load stays clean. */
static int install_tile(bool announce) {
    struct stat stx;
    if (stat(TILE_PKG_DISK, &stx) != 0 || stx.st_size <= 0)
        return 0;   /* nothing to install; not an error */

    int res = 0;
    int mod = sceKernelLoadStartModule(APPINST_SPRX, 0, NULL, 0, NULL, &res);
    if (mod < 0) { if (announce) notify("Tile: cannot load installer module"); return -1; }

    uint32_t h = 0;
    int (*ai_init)(void) = 0, (*ai_term)(void) = 0;
    int (*ai_installpkg)(const char *, void *) = 0;
    if (!kernel_dynlib_handle(-1, "libSceAppInstUtil.sprx", &h)) {
        ai_init       = (void *)kernel_dynlib_resolve(-1, h, NID_APPINST_INIT);
        ai_term       = (void *)kernel_dynlib_resolve(-1, h, NID_APPINST_TERM);
        ai_installpkg = (void *)kernel_dynlib_resolve(-1, h, NID_APPINST_PKG);
    }
    if (!ai_installpkg) { if (announce) notify("Tile: could not resolve installer"); return -1; }
    if (ai_init) ai_init();

    uint8_t pkg_info[256];
    memset(pkg_info, 0, sizeof(pkg_info));
    int rc = ai_installpkg(TILE_PKG_INSTALL, pkg_info);
    if (ai_term) ai_term();

    if (announce)
        notify(rc == 0 ? "Tile installed - check your dashboard" : "Tile install returned an error");
    return rc;
}

int main(void) {
    /* privilege escalation first (needed for the system installer) */
    jb_escalate_pid(getpid());
    sceUserServiceInitialize(0);   /* MUST run before the browser can launch */

    /* one-elf convenience: make sure the tile exists, then open the shop */
    install_tile(false);           /* quiet: only speaks up on an actual error */

#if PMS_AUTOOPEN
    notify(PMS_MSG_OPENING);
    sleep(1);
    open_shop(PMS_URL);
#endif

    int srv = socket(AF_INET, SOCK_STREAM, 0);
    if (srv < 0) return 1;
    int opt = 1;
    setsockopt(srv, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));

    struct sockaddr_in addr;
    memset(&addr, 0, sizeof(addr));
    addr.sin_family = AF_INET;
    addr.sin_addr.s_addr = INADDR_ANY;
    addr.sin_port = htons(CTRL_PORT);
    if (bind(srv, (struct sockaddr *)&addr, sizeof(addr)) < 0) return 1;
    listen(srv, 8);

    for (;;) {
        int cl = accept(srv, 0, 0);
        if (cl < 0) continue;
        char buf[1300];
        int n = read(cl, buf, sizeof(buf) - 1);
        if (n > 0) {
            buf[n] = 0;
            while (n && (buf[n - 1] == '\n' || buf[n - 1] == '\r')) buf[--n] = 0;
            if (!strncmp(buf, "notify ", 7)) {
                notify(buf + 7);
            } else if (!strncmp(buf, "open ", 5) || !strcmp(buf, "open")) {
                open_shop((buf[4] == ' ') ? buf + 5 : PMS_URL);
            } else if (!strcmp(buf, "installtile")) {
                install_tile(true);
            }
            /* "ping" -> just ack below */
        }
        write(cl, "ok\n", 3);
        close(cl);
    }
    return 0;
}

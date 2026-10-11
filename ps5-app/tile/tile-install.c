/*
 * PKG MUTANT SHOP - dashboard tile installer
 * ==========================================
 * Registers the homebrew app already placed on the console by deploy-tile.py.
 *
 * Method (mirrors the working dump_installer.elf on this console):
 *   - link libSceAppInstUtil as a load-time NEEDED dependency so elfldr sets up the
 *     module in the process at creation (loading it at RUNTIME - dlopen or
 *     LoadStartModule - faults; load-time linking is how dump_installer does it)
 *   - do NOT link libSceIpmi (that is what killed our earlier load before main)
 *   - call sceAppInstUtilInitialize / sceAppInstUtilAppInstallTitleDir directly
 *
 * Reports every step on-screen (sceNotificationSend) AND to the companion's
 * PS5-log listener on the PC (REPORT_HOST:9097) for headless debugging.
 */
#include <stdio.h>
#include <stdbool.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/socket.h>
#include <netinet/in.h>
#include <arpa/inet.h>
#include <unistd.h>

#define SCE_NOTIFICATION_LOCAL_USER_ID_SYSTEM 0xFE
int sceNotificationSend(int userId, bool isLogged, const char *payload);

/* linked as load-time NEEDED (libSceAppInstUtil.sprx), like dump_installer */
int sceAppInstUtilInitialize(void);
int sceAppInstUtilAppInstallTitleDir(const char *title_id, const char *dir, void *opt);

#ifndef TITLE_ID
#define TITLE_ID "PKGM00001"
#endif
#ifndef REPORT_HOST
#define REPORT_HOST "10.0.0.76"
#endif
#ifndef REPORT_PORT
#define REPORT_PORT 9097
#endif

static const char *TOAST_FMT =
  "{\"rawData\":{\"viewTemplateType\":\"InteractiveToastTemplateB\",\"channelType\":\"Downloads\","
  "\"useCaseId\":\"IDC\",\"toastOverwriteType\":\"No\",\"isImmediate\":true,\"priority\":100,"
  "\"viewData\":{\"icon\":{\"type\":\"Predefined\",\"parameters\":{\"icon\":\"download\"}},"
  "\"message\":{\"body\":\"%s\"},\"subMessage\":{\"body\":\"PKG MUTANT SHOP\"}},"
  "\"platformViews\":{\"previewDisabled\":{\"viewData\":"
  "{\"icon\":{\"type\":\"Predefined\",\"parameters\":{\"icon\":\"download\"}},"
  "\"message\":{\"body\":\"%s\"}}}}},\"localNotificationId\":\"7711\"}";

static void notify(const char *msg) {
    char json[2600], safe[512];
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

static void pc_log(const char *msg) {
    int s = socket(AF_INET, SOCK_STREAM, 0);
    if (s < 0) return;
    struct sockaddr_in a;
    memset(&a, 0, sizeof a);
    a.sin_family = AF_INET;
    a.sin_port = htons(REPORT_PORT);
    a.sin_addr.s_addr = inet_addr(REPORT_HOST);
    if (connect(s, (struct sockaddr *)&a, sizeof a) == 0) {
        char line[400];
        int n = snprintf(line, sizeof line, "[tile-install] %s\n", msg);
        write(s, line, n);
    }
    close(s);
    usleep(120000);   /* let the PC listener finish this connection before the next */
}

static void report(const char *msg) {
    notify(msg);
    pc_log(msg);
    /* also append to a file so the PC can read the result over FTP (no firewall dependency) */
    FILE *rf = fopen("/data/pkg-mutant-shop/register-result.txt", "a");
    if (rf) { fprintf(rf, "%s\n", msg); fclose(rf); }
}

static int exists(const char *p) {
    struct stat st;
    return stat(p, &st) == 0;
}

static const char *PARAM_JSON =
  "{\n"
  "    \"applicationCategoryType\": 0,\n"
  "    \"localizedParameters\": { \"defaultLanguage\": \"en-US\", \"en-US\": { \"titleName\": \"PKG MUTANT SHOP\" } },\n"
  "    \"titleId\": \"" TITLE_ID "\"\n"
  "}\n";

int main(void) {
    char buf[220];
    int err;

    report("Installing tile...");

    if (!exists("/user/app/" TITLE_ID "/sce_sys/param.json") ||
        !exists("/system_ex/app/" TITLE_ID "/eboot.bin")) {
        report("No app files on console - run  python deploy-tile.py  first, then re-run me");
        return -1;
    }

    if ((err = sceAppInstUtilInitialize())) {
        snprintf(buf, sizeof buf, "AppInstUtil init failed: 0x%x", (unsigned)err);
        report(buf);
        return -1;
    }
    report("AppInstUtil init OK, registering...");
    if ((err = sceAppInstUtilAppInstallTitleDir(TITLE_ID, "/user/app/", 0))) {
        snprintf(buf, sizeof buf, "Register failed: 0x%x (system_ex writable? MTRW)", (unsigned)err);
        report(buf);
        return -1;
    }

    /* write the system-side param.json AFTER registration (SDK sample ordering) */
    FILE *f = fopen("/system_ex/app/" TITLE_ID "/sce_sys/param.json", "w");
    if (f) {
        fputs(PARAM_JSON, f);
        fclose(f);
    }

    report("Tile installed! Look for PKG MUTANT SHOP on your dashboard");
    return 0;
}

/* Drives shared/pms_inflate.h, pms_zip.h and pms_zplan.h on the PC so they can be compared
 * against Python's own zlib and zipfile, and against companion/payloads.py's zip_plan().
 *
 *   inflate <in.deflate> <comp-len> <out.bin>   one raw deflate stream, streamed both ends
 *   list    <in.zip>                            one line per entry: method size name
 *   extract <in.zip> <dir>                      every entry, exactly as the console extracts it
 *   plan    <in.zip>                            what installing it would mean, as JSON
 *   install <in.zip> <root> [tid] [data-spec]   place it, exactly as the console places it
 *
 * The functions are the shipped ones; nothing here is a second implementation.
 * See tools/test_archive.py, which is the only reason to trust any of it.
 */
#include <stdio.h>
#include <stdlib.h>
#include <stdarg.h>
#include <string.h>
#include <strings.h>
#include <errno.h>
#include <unistd.h>
#include <fcntl.h>
#include <dirent.h>
#include <sys/stat.h>

#define SHOP_DATA_DIR "/tmp/pms-archive-harness"

static void mkparents(const char *path) {
    char tmp[1600];
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p != '/') continue;
        *p = 0;
        mkdir(tmp, 0777);
        *p = '/';
    }
}

/* The log both console builds write to. Here it goes to stderr, so a test run shows the same
   sentences the console would have recorded. */
static void ilog(const char *fmt, ...) {
    va_list ap;
    va_start(ap, fmt);
    fputs("  [log] ", stderr);
    vfprintf(stderr, fmt, ap);
    fputc('\n', stderr);
    va_end(ap);
}

/* The page's own JSON reader, which the planner uses for param.json's titleId. The console builds
   both have one; this is the PS5's, copied so the harness stands alone. */
static const char *json_str_after(const char *p, const char *key, char *out, size_t outsz) {
    char pat[48];
    snprintf(pat, sizeof(pat), "\"%s\"", key);
    const char *k = strstr(p, pat);
    out[0] = 0;
    if (!k) return NULL;
    k += strlen(pat);
    while (*k == ' ' || *k == '\t' || *k == '\n' || *k == '\r') k++;
    if (*k != ':') return NULL;
    k++;
    while (*k == ' ' || *k == '\t' || *k == '\n' || *k == '\r') k++;
    if (*k != '"') return NULL;
    k++;
    size_t j = 0;
    while (*k && *k != '"' && j < outsz - 1) out[j++] = *k++;
    out[j] = 0;
    return out;
}

#include "../shared/pms_inflate.h"
#include "../shared/pms_zip.h"
#include "../shared/pms_zplan.h"
#include "../shared/pms_install.h"

static void jstr(const char *s) {
    putchar('"');
    for (; *s; s++) {
        if (*s == '"' || *s == '\\') { putchar('\\'); putchar(*s); }
        else if ((unsigned char)*s < 0x20) printf("\\u%04x", *s);
        else putchar(*s);
    }
    putchar('"');
}

static int g_ok, g_bad;
static char g_dir[900];

/* zip_extract_one takes the FULL PATH of the file to write, not the folder to write into - the
   caller owns the name, which is what lets the console place an app folder somewhere of its own
   choosing. Handing it the directory instead made every entry write to "<dir>.part" and then try
   to rename that over the directory, which is 2751 identical ZIP_ERR_WRITEs and no clue why. */
static int walk_extract(const zip_ent_t *e, void *ctx) {
    int fd = *(int *)ctx;
    char nm[600];
    snprintf(nm, sizeof(nm), "%s", e->name);
    if (!zip_name_ok(nm)) {
        printf("REFUSED %s\n", e->name);
        g_bad++;
        return 0;
    }
    char out[1500];
    snprintf(out, sizeof(out), "%s/%s", g_dir, nm);
    if (e->is_dir) { mkparents(out); mkdir(out, 0777); return 0; }
    int rc = zip_extract_one(fd, e, out);
    if (rc == ZIP_OK) g_ok++;
    else { g_bad++; fprintf(stderr, "  %s -> rc=%d\n", e->name, rc); }
    return 0;
}

static int walk_list(const zip_ent_t *e, void *ctx) {
    (void)ctx;
    printf("%d %lld %s\n", e->method, e->uncomp, e->name);
    return 0;
}

int main(int argc, char **argv) {
    if (argc < 3) {
        fprintf(stderr, "usage: archive_harness inflate|list|extract|plan ...\n");
        return 2;
    }
    if (!strcmp(argv[1], "inflate")) {
        if (argc < 5) return 2;
        int fi = open(argv[2], O_RDONLY);
        if (fi < 0) return 2;
        int fo = open(argv[4], O_WRONLY | O_CREAT | O_TRUNC, 0666);
        if (fo < 0) { close(fi); return 2; }
        long long out = 0;
        int rc = inf_run(fi, atoll(argv[3]), fo, &out);
        close(fi);
        close(fo);
        printf("rc=%d out=%lld\n", rc, out);
        return rc == INF_OK ? 0 : 1;
    }

    int fd = open(argv[2], O_RDONLY);
    if (fd < 0) { fprintf(stderr, "open\n"); return 2; }
    struct stat st;
    fstat(fd, &st);

    if (!strcmp(argv[1], "list")) {
        int rc = zip_walk(fd, (long long)st.st_size, walk_list, NULL);
        close(fd);
        printf("walk_rc=%d\n", rc);
        return rc == ZIP_OK ? 0 : 1;
    }
    if (!strcmp(argv[1], "extract")) {
        if (argc < 4) return 2;
        snprintf(g_dir, sizeof(g_dir), "%s", argv[3]);
        int rc = zip_walk(fd, (long long)st.st_size, walk_extract, &fd);
        close(fd);
        printf("extracted=%d failed=%d walk_rc=%d\n", g_ok, g_bad, rc);
        return (rc == ZIP_OK && !g_bad) ? 0 : 1;
    }
    if (!strcmp(argv[1], "install")) {
        close(fd);
        if (argc < 4) return 2;
        arinst_t r;
        int rc = archive_install(argv[2], argv[3], argc > 4 ? argv[4] : "",
                                 argc > 5 ? argv[5] : "", &r, NULL);
        printf("{\"rc\":%d,\"ok\":%s,\"kind\":", rc, r.ok ? "true" : "false");
        jstr(r.kind);
        printf(",\"tid\":"); jstr(r.tid);
        printf(",\"placed\":"); jstr(r.placed);
        printf(",\"pkg\":"); jstr(r.pkg);
        printf(",\"why\":"); jstr(r.why);
        printf(",\"files\":%lld,\"bytes\":%lld,\"skipped\":[", r.files, r.bytes);
        for (int i = 0; i < r.n_skipped; i++) { if (i) putchar(','); jstr(r.skipped[i]); }
        printf("]}\n");
        return rc == 0 ? 0 : 1;
    }
    if (!strcmp(argv[1], "plan")) {
        zplan_t p;
        int rc = zip_plan(fd, (long long)st.st_size, &p);
        close(fd);
        printf("{\"rc\":%d,\"ok\":%s,\"kind\":", rc, p.ok ? "true" : "false");
        jstr(p.kind);
        printf(",\"why\":"); jstr(p.why);
        printf(",\"app_root\":"); jstr(p.app_root);
        printf(",\"tid\":"); jstr(p.tid);
        printf(",\"entries\":%lld", p.entries);
        printf(",\"unsafe\":"); jstr(p.unsafe);
        printf(",\"pkgs\":[");
        for (int i = 0; i < p.n_pkgs; i++) { if (i) putchar(','); jstr(p.pkgs[i]); }
        printf("],\"data_roots\":[");
        for (int i = 0; i < p.n_data; i++) { if (i) putchar(','); jstr(p.data_roots[i]); }
        printf("]}\n");
        return 0;
    }
    close(fd);
    fprintf(stderr, "unknown mode\n");
    return 2;
}

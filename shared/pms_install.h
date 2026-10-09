/* pms_install.h - TURNING AN ARCHIVE ON THE CONSOLE INTO AN INSTALLED THING.
 *
 * This is the step the console never had. It could already fetch a release asset over TLS; what it
 * could not do was open the .zip that seven of the ten PS5 homebrews ship as, so the panel offered
 * a link to the project's GitHub page instead of a button - which is the "it redirects us to their
 * github release page instead of doing everything on its own" report, exactly.
 *
 * WHAT IT DOES, AND NOTHING MORE. The planner (pms_zplan.h) finds the ONE thing in the archive
 * that has to be installed: a .pkg, or the app folder with eboot.bin and sce_sys/param.json in it.
 * That is placed, and everything else in the archive is copied across untouched. An archive INSIDE
 * the app - PS5X360 ships a Kinect-Phone.zip - belongs to the emulator; it is a file like any
 * other file and it is not opened.
 *
 * IT MERGES. IT DOES NOT REPLACE. This is the decision in here that matters most, and it is not
 * the obvious one. Deleting the destination folder first is simpler and it is what the PC lane
 * does (_replace_dir keeps a .old copy, so there it is recoverable) - but on the console there is
 * no second copy, and these emulators keep the owner's own files inside their app folder: PS5X360
 * reads games from assets/roms/ inside the app, and RetroArch keeps its entire configuration,
 * saves and save states in there. A replace would be an update that silently destroyed a ROM
 * library. So every file the archive carries is written over the file of the same name, and
 * anything the destination has that the archive does not is left exactly where it is.
 *
 * Each file lands through a .part and a rename, so a reader never sees a half-written one, and
 * rename() on these consoles will not replace an existing file - hence the unlink first. If the
 * run is interrupted the folder holds a mixture of two versions, which running the update again
 * repairs; the alternative is a folder holding nothing, which nothing repairs.
 *
 * A DESTINATION IS NEVER INVENTED. An emulator's external data directory (PS5SX2's /data/PCSX2)
 * comes from the catalogue, through `data_spec`. A directory in the archive that the catalogue
 * does not name is reported by name and skipped - this app has been bitten before by guessing
 * where something belongs.
 *
 * Needs from the host: pms_zip.h, pms_zplan.h, mkparents(), ilog().
 */
#ifndef PMS_INSTALL_H
#define PMS_INSTALL_H

#define AR_MAX_SKIP 6

typedef struct {
    int  ok;
    char kind[8];                  /* what the planner decided: pkg / app / data / unknown */
    char tid[20];
    /* 1200, not 700. A destination is a root plus a title id, and an extracted package is the
       archive's own directory plus a name the archive chose - which pms_zplan.h allows up to 400
       characters of. 700 made the compiler point out that the two could not both fit. */
    char placed[1200];             /* where the app folder was merged into */
    char pkg[1200];                /* the package that was extracted, for the caller to install */
    char why[260];                 /* a sentence, when it refused or had to skip something */
    char skipped[AR_MAX_SKIP][96]; /* directories the catalogue did not name a home for */
    int  n_skipped;
    long long files;               /* how many were written */
    long long bytes;
} arinst_t;

/* ---- the walk ------------------------------------------------------------------------------- */

typedef struct {
    int         fd;
    const char *prefix;            /* only entries under this, "" for all of them */
    size_t      plen;
    const char *into;              /* where the prefix's contents go */
    arinst_t   *out;
    long long   total;             /* how many THIS pass will write, counted first */
    long long   done;              /* how many it has written. PER PASS, not running: an archive
                                      with a data directory makes two passes, and reporting the
                                      running total against one pass's count put the console's own
                                      progress bar at 110% - observed, on the real thing. */
    void      (*prog)(long long, long long);
    int         rc;
} arwalk_t;

/* Count the entries a pass will write, so the progress the owner watches is a real fraction. */
static int ar_count(const zip_ent_t *e, void *vp) {
    arwalk_t *w = (arwalk_t *)vp;
    if (e->is_dir) return 0;
    if (w->plen && strncmp(e->name, w->prefix, w->plen)) return 0;
    w->total++;
    return 0;
}

static int ar_place(const zip_ent_t *e, void *vp) {
    arwalk_t *w = (arwalk_t *)vp;
    if (e->is_dir) return 0;
    if (w->plen && strncmp(e->name, w->prefix, w->plen)) return 0;
    const char *rel = e->name + w->plen;
    while (*rel == '/') rel++;
    if (!*rel) return 0;
    /* LOOSE FILES AT THE TOP OF THE ARCHIVE ARE NOT PART OF THE APP. ProsperoEden ships
       README.md, LICENSE and THIRD_PARTY_NOTICES.md beside its PPSA99008/ folder; with no prefix
       to strip they would be copied into the app folder, where they mean nothing. Only entries
       under the prefix are placed, which is what the strncmp above already decides - this is the
       case where the prefix is empty and the plan said `app`, so a name with no slash is a loose
       file. */
    if (!w->plen && !strchr(rel, '/')) return 0;

    char nm[600];
    snprintf(nm, sizeof(nm), "%s", rel);
    if (!zip_name_ok(nm)) {
        ilog("archive: refused %s - it wants out of the folder", e->name);
        w->rc = ZIP_ERR_PATH;
        return 0;
    }
    char dst[1400];
    snprintf(dst, sizeof(dst), "%s/%s", w->into, nm);
    int rc = zip_extract_one(w->fd, e, dst);
    if (rc != ZIP_OK) {
        ilog("archive: %s failed (%d)", e->name, rc);
        w->rc = rc;
        return rc;                                 /* stop: a bad archive is not half-installed */
    }
    w->out->files++;
    w->out->bytes += e->uncomp > 0 ? e->uncomp : 0;
    w->done++;
    if (w->prog && w->total > 0) w->prog(w->done, w->total);
    return 0;
}

/* Extract everything under `prefix` into `into`, merging. Returns ZIP_OK or the first failure. */
static int ar_subtree(int fd, long long fsize, const char *prefix, const char *into,
                      arinst_t *out, void (*prog)(long long, long long)) {
    arwalk_t w;
    memset(&w, 0, sizeof(w));
    w.fd = fd;
    w.prefix = prefix ? prefix : "";
    w.plen = strlen(w.prefix);
    w.into = into;
    w.out = out;
    w.prog = prog;
    zip_walk(fd, fsize, ar_count, &w);
    long long want = w.total;
    mkparents(into);
    mkdir(into, 0777);
    int rc = zip_walk(fd, fsize, ar_place, &w);
    if (rc == ZIP_OK) rc = w.rc;
    if (rc == ZIP_OK && want > 0 && out->files <= 0) rc = ZIP_ERR_ENTRY;
    return rc;
}

/* ONE ENTRY, BY NAME, to an exact path. */
typedef struct {
    int         fd;
    const char *want;
    const char *dst;
    arinst_t   *out;
    void      (*prog)(long long, long long);
    int         rc;
    int         found;
} arone_t;

static int ar_one_cb(const zip_ent_t *e, void *vp) {
    arone_t *w = (arone_t *)vp;
    if (e->is_dir || w->found || strcmp(e->name, w->want)) return 0;
    w->found = 1;
    w->rc = zip_extract_one(w->fd, e, w->dst);
    if (w->rc == ZIP_OK) {
        w->out->files++;
        w->out->bytes += e->uncomp > 0 ? e->uncomp : 0;
        if (w->prog) w->prog(1, 1);
    }
    return 0;
}

static int ar_one_file(int fd, long long fsize, const char *want, const char *dst,
                       arinst_t *out, void (*prog)(long long, long long)) {
    arone_t w;
    memset(&w, 0, sizeof(w));
    w.fd = fd;
    w.want = want;
    w.dst = dst;
    w.out = out;
    w.prog = prog;
    char dir[1400];
    snprintf(dir, sizeof(dir), "%s", dst);
    char *sl = strrchr(dir, '/');
    if (sl) { *sl = 0; mkparents(dir); mkdir(dir, 0777); }
    int rc = zip_walk(fd, fsize, ar_one_cb, &w);
    if (rc != ZIP_OK) return rc;
    if (!w.found) return ZIP_ERR_ENTRY;
    return w.rc;
}

/* Where does `name` belong, according to the catalogue? `spec` is "NAME:/path,NAME:/path".
   Returns 1 and fills `dst` when the catalogue names it, 0 when it does not. */
static int ar_data_dest(const char *spec, const char *name, char *dst, size_t dstsz) {
    if (!spec || !*spec || !name || !*name) return 0;
    size_t nl = strlen(name);
    const char *p = spec;
    while (*p) {
        const char *comma = strchr(p, ',');
        const char *end = comma ? comma : p + strlen(p);
        const char *colon = NULL;
        for (const char *q = p; q < end; q++) if (*q == ':') { colon = q; break; }
        if (colon && (size_t)(colon - p) == nl && !strncmp(p, name, nl)) {
            size_t vl = (size_t)(end - colon - 1);
            if (vl > 0 && vl < dstsz && colon[1] == '/') {
                memcpy(dst, colon + 1, vl);
                dst[vl] = 0;
                return 1;
            }
            return 0;
        }
        if (!comma) break;
        p = comma + 1;
    }
    return 0;
}

/* ---- the whole job --------------------------------------------------------------------------- */

/* Install the archive at `zip`.
 *
 *   root       where app folders live on the chosen drive, e.g. /data/homebrew or
 *              /mnt/usb0/homebrew. The caller decides the drive; this never does.
 *   want_tid   the title id the catalogue expects, or "" to take the planner's. When both are
 *              present and they DISAGREE, that is a refusal: the catalogue and the release are
 *              describing different things and placing either one would be a guess.
 *   data_spec  "PCSX2:/data/PCSX2" - from the catalogue, may be "".
 *
 * Returns 0 on success. On any other answer `out->why` is a sentence for the owner.
 */
static int archive_install(const char *zip, const char *root, const char *want_tid,
                           const char *data_spec, arinst_t *out,
                           void (*prog)(long long, long long)) {
    memset(out, 0, sizeof(*out));
    struct stat st;
    if (stat(zip, &st) != 0 || !S_ISREG(st.st_mode) || st.st_size <= 0) {
        snprintf(out->why, sizeof(out->why), "That archive is not on the console any more.");
        return ZIP_ERR_OPEN;
    }
    int fd = open(zip, O_RDONLY);
    if (fd < 0) {
        snprintf(out->why, sizeof(out->why), "That archive would not open.");
        return ZIP_ERR_OPEN;
    }
    long long fsize = (long long)st.st_size;

    /* THE PLAN IS A STATIC. zplan_t is about 13 KB and this runs on a worker thread with a 256 KB
       stack that also has to hold the extractor's buffers. One archive is installed at a time -
       the job record enforces it - so one plan is enough. */
    static zplan_t plan;
    int rc = zip_plan(fd, fsize, &plan);
    if (rc != ZIP_OK || !plan.ok) {
        snprintf(out->why, sizeof(out->why), "%s",
                 plan.why[0] ? plan.why : "This archive is not one this console can install.");
        close(fd);
        return rc != ZIP_OK ? rc : ZIP_ERR_FORMAT;
    }
    snprintf(out->kind, sizeof(out->kind), "%s", plan.kind);
    snprintf(out->tid, sizeof(out->tid), "%s", plan.tid);
    ilog("archive: %s is %s, %lld entries, tid=%s root=%s",
         zip, plan.kind, plan.entries, plan.tid[0] ? plan.tid : "-",
         plan.app_root[0] ? plan.app_root : "-");

    /* A PACKAGE: extract it beside the archive and let the caller's own installer have it. There
       is one install lane on this console and this does not become a second one. */
    if (!strcmp(plan.kind, "pkg")) {
        if (plan.n_pkgs < 1) {
            snprintf(out->why, sizeof(out->why), "The archive says it holds a package and does "
                                                 "not.");
            close(fd);
            return ZIP_ERR_FORMAT;
        }
        char dir[700];
        snprintf(dir, sizeof(dir), "%s", zip);
        char *sl = strrchr(dir, '/');
        if (sl) *sl = 0; else snprintf(dir, sizeof(dir), ".");
        const char *base = strrchr(plan.pkgs[0], '/');
        base = base ? base + 1 : plan.pkgs[0];
        snprintf(out->pkg, sizeof(out->pkg), "%s/%s", dir, base);
        /* ONE NAMED FILE, not a subtree: ar_subtree strips the prefix from each name, and for a
           single file the prefix IS the whole name, so it would strip the name to nothing and
           place no file at all. */
        rc = ar_one_file(fd, fsize, plan.pkgs[0], out->pkg, out, prog);
        close(fd);
        if (rc == ZIP_OK && out->files > 0) { out->ok = 1; return 0; }
        snprintf(out->why, sizeof(out->why), "%s",
                 rc == ZIP_ERR_CRC ? "The package inside that archive is damaged."
                                   : "The package inside the archive would not extract.");
        return rc != ZIP_OK ? rc : ZIP_ERR_ENTRY;
    }

    if (strcmp(plan.kind, "app")) {
        snprintf(out->why, sizeof(out->why), "%s",
                 plan.why[0] ? plan.why
                             : "There is no app and no package inside that archive.");
        close(fd);
        return ZIP_ERR_FORMAT;
    }

    /* THE TITLE ID DECIDES THE FOLDER, so the two sources for it have to agree. */
    const char *tid = plan.tid;
    if (want_tid && want_tid[0]) {
        if (plan.tid[0] && strcmp(plan.tid, want_tid)) {
            snprintf(out->why, sizeof(out->why),
                     "That release is %s and this entry is %s - they are different titles, so "
                     "nothing was changed.", plan.tid, want_tid);
            close(fd);
            return ZIP_ERR_FORMAT;
        }
        tid = want_tid;
    }
    if (!tid || !tid[0]) {
        snprintf(out->why, sizeof(out->why),
                 "Nothing in that archive says which title it is, so there is no folder to put "
                 "it in.");
        close(fd);
        return ZIP_ERR_FORMAT;
    }

    char into[700];
    snprintf(into, sizeof(into), "%s/%s", root, tid);
    snprintf(out->placed, sizeof(out->placed), "%s", into);
    rc = ar_subtree(fd, fsize, plan.app_root, into, out, prog);
    if (rc != ZIP_OK) {
        snprintf(out->why, sizeof(out->why),
                 rc == ZIP_ERR_CRC   ? "Part of that archive is damaged - it was not finished."
               : rc == ZIP_ERR_WRITE ? "The console ran out of room, or that drive is read-only."
               : rc == ZIP_ERR_PATH  ? "That archive tried to write outside its own folder."
                                     : "That archive would not extract.");
        close(fd);
        return rc;
    }

    /* THE EMULATOR'S OWN DATA DIRECTORIES, where - and only where - the catalogue says. */
    for (int i = 0; i < plan.n_data; i++) {
        const char *top = plan.data_roots[i];
        const char *leaf = strrchr(top, '/');
        leaf = leaf ? leaf + 1 : top;
        char dest[700];
        if (!ar_data_dest(data_spec, leaf, dest, sizeof(dest))) {
            if (out->n_skipped < AR_MAX_SKIP)
                snprintf(out->skipped[out->n_skipped++], sizeof(out->skipped[0]), "%s", leaf);
            ilog("archive: %s has no recorded home, so it was left in the archive", leaf);
            continue;
        }
        int drc = ar_subtree(fd, fsize, top, dest, out, prog);
        if (drc != ZIP_OK) {
            ilog("archive: %s -> %s failed (%d)", leaf, dest, drc);
            if (out->n_skipped < AR_MAX_SKIP)
                snprintf(out->skipped[out->n_skipped++], sizeof(out->skipped[0]), "%s", leaf);
        }
    }
    close(fd);

    if (out->n_skipped) {
        /* NAMED, NOT COUNTED. "Something was skipped" is not an answer anyone can act on. */
        int n = snprintf(out->why, sizeof(out->why),
                         "Installed. These folders in the archive have no recorded place on the "
                         "console, so they were left alone: ");
        for (int i = 0; i < out->n_skipped && n > 0 && n < (int)sizeof(out->why) - 2; i++)
            n += snprintf(out->why + n, sizeof(out->why) - (size_t)n, "%s%s",
                          i ? ", " : "", out->skipped[i]);
    }
    out->ok = 1;
    return 0;
}

#endif /* PMS_INSTALL_H */

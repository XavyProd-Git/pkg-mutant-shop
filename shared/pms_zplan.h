/* pms_zplan.h - what is INSIDE an archive, and therefore what installing it means.
 *
 * The same four answers companion/payloads.py's zip_plan() gives, decided by the same rules, so
 * that a console with no PC reaches the same conclusion a PC would. tools/test_archive.py runs
 * both over every real release archive and compares every field; two planners that could drift
 * are only tolerable while something holds them together, and that is the something.
 *
 * THE EMULATORS' OWN FILES ARE NOT OUR BUSINESS. This looks for the one thing that has to be
 * installed - a .pkg, or the app folder with eboot.bin and sce_sys/param.json in it - and stops.
 * An archive inside the app (PS5X360 ships a Kinect-Phone.zip) belongs to the emulator and is
 * copied across untouched, exactly as the release shipped it.
 *
 * Needs from the host: pms_zip.h, json_str_after(), SHOP_DATA_DIR.
 */
#ifndef PMS_ZPLAN_H
#define PMS_ZPLAN_H

/* ---------------- what is inside an archive, and where each part belongs -------------------
 *
 * The same four answers payload_engine.zip_plan() gives on a PC, decided by the same rules, so
 * that an emulator installs the same way whether a companion is running or not. There is a test
 * that runs both over every real release archive and requires identical answers; two planners
 * that could drift are only tolerable while something holds them together.
 *
 *   pkg      the archive carries packages - the installer reads them and decides, as for a game
 *   app      an app folder (eboot.bin with sce_sys/param.json beside it) at any depth, possibly
 *            inside a wrapper directory, possibly with data trees beside it
 *   data     files, but no app and no package: a data drop for something already installed
 *   unknown  nothing recognisable, which is reported rather than guessed at
 *
 * READ FROM THE CENTRAL DIRECTORY, so it costs the same for RetroArch's 752 MB as for Porpoise's
 * 30 MB: names and sizes, nothing inflated. The PC holds every name in a set and tests membership;
 * a payload cannot (60000 names is tens of megabytes), so this walks the directory again for each
 * question instead. The directory is a few KB and there are at most a dozen questions.
 * ------------------------------------------------------------------------------------------- */

#define ZP_MAX_PKGS  8
#define ZP_MAX_DATA  12
#define ZP_MAX_ROOTS 8

typedef struct {
    int       ok;
    char      kind[8];                      /* pkg | app | data | "" */
    char      why[200];
    char      app_root[400];
    char      tid[20];
    int       n_pkgs;
    char      pkgs[ZP_MAX_PKGS][400];
    int       n_data;
    char      data_roots[ZP_MAX_DATA][400];
    long long entries;
    char      unsafe[400];                  /* the first name that wanted out of the folder */
} zplan_t;

/* ---- the little walkers, one question each ---- */

typedef struct {
    zplan_t *p;
    /* question 1: count, safety, packages */
    /* question 2: directories that hold an eboot.bin */
    char   roots[ZP_MAX_ROOTS][400];
    int    n_roots;
    /* question 3: does one exact name exist */
    const char *want;
    int    found;
    /* question 4: top-level names under a prefix */
    const char *pre;
    size_t prelen;
    const char *skip;
} zp_ctx_t;

static int zp_scan(const zip_ent_t *e, void *vp) {
    zp_ctx_t *c = (zp_ctx_t *)vp;
    zplan_t *p = c->p;
    p->entries++;
    char nm[600];
    snprintf(nm, sizeof(nm), "%s", e->name);
    if (!zip_name_ok(nm)) {
        if (!p->unsafe[0]) snprintf(p->unsafe, sizeof(p->unsafe), "%.*s",
                                    (int)sizeof(p->unsafe) - 1, e->name);
        return 0;
    }
    /* TOO LONG TO RECORD IS TOO LONG TO ACT ON. The fields in zplan_t are 400 bytes and a ZIP
       name may be 600, so a longer one would be silently TRUNCATED into them - and a truncated
       path is not a shorter path, it is a different one, pointing somewhere nobody chose. Refused
       as unsafe instead, which is the same answer this gives a name trying to escape the folder.
       Measured: the longest name in all six real release archives is 136 characters. */
    if (strlen(nm) >= sizeof(p->app_root) - 1) {
        if (!p->unsafe[0]) snprintf(p->unsafe, sizeof(p->unsafe), "%.*s",
                                    (int)sizeof(p->unsafe) - 1, e->name);
        return 0;
    }
    if (e->is_dir) return 0;
    size_t n = strlen(nm);
    /* A PACKAGE WINS: it is the lane with the fewest unknowns. */
    if (n > 4 && !strcasecmp(nm + n - 4, ".pkg") && p->n_pkgs < ZP_MAX_PKGS)
        snprintf(p->pkgs[p->n_pkgs++], sizeof(p->pkgs[0]), "%s", nm);
    /* An app folder announces itself with an eboot.bin. */
    if (n > 10 && !strcmp(nm + n - 10, "/eboot.bin") && c->n_roots < ZP_MAX_ROOTS) {
        size_t dl = n - 10;
        if (dl < sizeof(c->roots[0])) {
            memcpy(c->roots[c->n_roots], nm, dl);
            c->roots[c->n_roots][dl] = 0;
            c->n_roots++;
        }
    }
    return 0;
}

static int zp_exists(const zip_ent_t *e, void *vp) {
    zp_ctx_t *c = (zp_ctx_t *)vp;
    char nm[600];
    snprintf(nm, sizeof(nm), "%s", e->name);
    for (char *q = nm; *q; q++) if (*q == '\\') *q = '/';
    if (!strcmp(nm, c->want)) { c->found = 1; return 1; }
    return 0;
}

static int zp_tops(const zip_ent_t *e, void *vp) {
    zp_ctx_t *c = (zp_ctx_t *)vp;
    zplan_t *p = c->p;
    char nm[600];
    snprintf(nm, sizeof(nm), "%s", e->name);
    for (char *q = nm; *q; q++) if (*q == '\\') *q = '/';
    if (e->is_dir) return 0;
    if (c->prelen && strncmp(nm, c->pre, c->prelen)) return 0;
    const char *rest = nm + c->prelen;
    const char *slash = strchr(rest, '/');
    if (!slash) return 0;                                  /* a loose file, not a tree */
    char top[400];
    size_t tl = (size_t)(slash - rest);
    if (tl == 0 || tl >= sizeof(top) - c->prelen - 1) return 0;
    snprintf(top, sizeof(top), "%.*s%.*s", (int)c->prelen, c->pre, (int)tl, rest);
    if (c->skip && !strcmp(top, c->skip)) return 0;
    for (int i = 0; i < p->n_data; i++) if (!strcmp(p->data_roots[i], top)) return 0;
    if (p->n_data < ZP_MAX_DATA) snprintf(p->data_roots[p->n_data++], sizeof(p->data_roots[0]), "%s", top);
    return 0;
}

static int zp_has(int fd, long long fsize, const char *name) {
    zp_ctx_t c;
    memset(&c, 0, sizeof(c));
    c.want = name;
    zip_walk(fd, fsize, zp_exists, &c);
    return c.found;
}

/* Read one small member into a caller buffer. Used for sce_sys/param.json, which is where the
   title id lives when the folder is not named after it. */
static int zp_read_member(int fd, long long fsize, const char *name, char *out, size_t outsz) {
    zip_ent_t want;
    int got = 0;
    memset(&want, 0, sizeof(want));
    /* Walked here rather than through zip_walk, because this wants the entry itself and the
       callback can only report. The directory is a few KB, so a second pass costs nothing. */
    long long off = 0, count = 0;
    if (zip_find_dir(fd, fsize, &off, &count)) return 0;
    for (long long i = 0; i < count; i++) {
        unsigned char h[46];
        if (!zip_pread(fd, h, sizeof(h), off)) return 0;
        if (!(h[0] == 0x50 && h[1] == 0x4B && h[2] == 0x01 && h[3] == 0x02)) return 0;
        unsigned nlen = zl16(h + 28), elen = zl16(h + 30), clen = zl16(h + 32);
        char nm[600];
        if (nlen && nlen < sizeof(nm) && zip_pread(fd, nm, nlen, off + 46)) {
            nm[nlen] = 0;
            for (char *p2 = nm; *p2; p2++) if (*p2 == '\\') *p2 = '/';
            if (!strcmp(nm, name)) {
                want.method = zl16(h + 10);
                want.crc    = zl32(h + 16);
                want.comp   = (long long)zl32(h + 20);
                want.uncomp = (long long)zl32(h + 24);
                want.lho    = (long long)zl32(h + 42);
                snprintf(want.name, sizeof(want.name), "%s", nm);
                got = 1;
                break;
            }
        }
        off += 46 + nlen + elen + clen;
        if (off >= fsize) break;
    }
    if (!got || want.uncomp <= 0 || want.uncomp > (long long)outsz - 1) return 0;
    char tmp[700];
    snprintf(tmp, sizeof(tmp), "%s/.zp-member", SHOP_DATA_DIR);
    if (zip_extract_one(fd, &want, tmp) != 0) return 0;
    int f = open(tmp, O_RDONLY);
    if (f < 0) { unlink(tmp); return 0; }
    ssize_t r = read(f, out, outsz - 1);
    close(f);
    unlink(tmp);
    if (r <= 0) return 0;
    out[r] = 0;
    return 1;
}

static int zp_tid_shape(const char *s) {
    /* PPSA / CUSA / PLAS / NPXS followed by four or five digits, which is what a PS4 or PS5
       title id looks like and what the folder is usually named. */
    size_t n = strlen(s);
    if (n < 8 || n > 9) return 0;
    if (strncasecmp(s, "PPSA", 4) && strncasecmp(s, "CUSA", 4) &&
        strncasecmp(s, "PLAS", 4) && strncasecmp(s, "NPXS", 4)) return 0;
    for (size_t i = 4; i < n; i++) if (s[i] < '0' || s[i] > '9') return 0;
    return 1;
}

static int zip_plan(int fd, long long fsize, zplan_t *p) {
    memset(p, 0, sizeof(*p));
    zp_ctx_t c;
    memset(&c, 0, sizeof(c));
    c.p = p;
    int rc = zip_walk(fd, fsize, zp_scan, &c);
    if (rc) { snprintf(p->why, sizeof(p->why), "the archive could not be read"); return rc; }
    if (p->unsafe[0]) {
        snprintf(p->why, sizeof(p->why), "it wants to write outside the folder");
        return 0;
    }
    if (p->n_pkgs) {
        p->ok = 1;
        snprintf(p->kind, sizeof(p->kind), "pkg");
        return 0;
    }
    /* An app folder: eboot.bin with sce_sys/param.json beside it, and failing that eboot.bin
       alone - a missing param.json only costs us the title, not the install. */
    int chosen = -1;
    for (int i = 0; i < c.n_roots && chosen < 0; i++) {
        char probe[600];
        snprintf(probe, sizeof(probe), "%s/sce_sys/param.json", c.roots[i]);
        if (zp_has(fd, fsize, probe)) chosen = i;
    }
    if (chosen < 0 && c.n_roots) chosen = 0;
    if (chosen >= 0) {
        snprintf(p->app_root, sizeof(p->app_root), "%s", c.roots[chosen]);
        const char *base = strrchr(p->app_root, '/');
        base = base ? base + 1 : p->app_root;
        if (zp_tid_shape(base)) {
            snprintf(p->tid, sizeof(p->tid), "%s", base);
            for (char *q = p->tid; *q; q++) if (*q >= 'a' && *q <= 'z') *q -= 32;
        } else {
            char pj[4096], probe[600], tid[64];
            snprintf(probe, sizeof(probe), "%s/sce_sys/param.json", p->app_root);
            if (zp_read_member(fd, fsize, probe, pj, sizeof(pj)) &&
                json_str_after(pj, "titleId", tid, sizeof(tid))) {
                snprintf(p->tid, sizeof(p->tid), "%s", tid);
                for (char *q = p->tid; *q; q++) if (*q >= 'a' && *q <= 'z') *q -= 32;
            }
        }
        /* WHAT ELSE TRAVELLED WITH IT. PS5SX2 ships PCSX2/ beside PPSA99203/ inside one wrapper
           directory; those are the console's data, not part of the app, and copying them into
           the app folder would put PCSX2/ inside the mounted title. */
        char wrap[400];
        snprintf(wrap, sizeof(wrap), "%s", p->app_root);
        char *sl = strrchr(wrap, '/');
        char pre[402];
        if (sl) { *sl = 0; snprintf(pre, sizeof(pre), "%s/", wrap); }
        else      pre[0] = 0;
        c.pre = pre;
        c.prelen = strlen(pre);
        c.skip = p->app_root;
        zip_walk(fd, fsize, zp_tops, &c);
        p->ok = 1;
        snprintf(p->kind, sizeof(p->kind), "app");
        return 0;
    }
    if (p->entries > 0) {
        p->ok = 1;
        snprintf(p->kind, sizeof(p->kind), "data");
        /* Every top-level tree, since there is no app to sit beside. */
        c.pre = "";
        c.prelen = 0;
        c.skip = NULL;
        zip_walk(fd, fsize, zp_tops, &c);
        return 0;
    }
    snprintf(p->why, sizeof(p->why), "there is nothing in it");
    return 0;
}

#endif /* PMS_ZPLAN_H */

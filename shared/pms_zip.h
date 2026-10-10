/* pms_zip.h - the ZIP container, on top of pms_inflate.h. Included by both console builds.
 *
 * Central directory (with ZIP64), CRC-32 on every entry, and a name check that refuses anything
 * trying to write outside the folder it was handed. One implementation; see pms_inflate.h for
 * why this is a header rather than a library, and tools/test_archive.py for the comparison against
 * Python's zipfile over every real release archive this app offers.
 *
 * Needs from the host: pms_inflate.h, mkparents(), SHOP_DATA_DIR.
 */
#ifndef PMS_ZIP_H
#define PMS_ZIP_H

/* ---------------- the archive itself --------------------------------------------------------
 *
 * The container around the deflate streams above: the ZIP central directory, which is the only
 * part of the format worth trusting. The local headers repeat most of it and are allowed to
 * disagree (a streaming writer sets the sizes to zero there and puts them in a trailing data
 * descriptor instead), so sizes and names come from the central directory and the local header
 * is read only to find where the data actually starts.
 *
 * ZIP64 is handled because the archives this opens are already 268 MB and the next one will be
 * bigger: when a size or an offset reads 0xFFFFFFFF the real value is in the entry's extra
 * field, and an archive written past 4 GB or 65535 entries has a second end-of-directory record.
 *
 * CRC-32 is checked on every entry. An emulator that half-extracted and then ran is a worse
 * outcome than one that refused, and the whole point of doing this on the console is that nobody
 * is watching it happen.
 * ------------------------------------------------------------------------------------------- */

#define ZIP_OK           0
#define ZIP_ERR_OPEN   -10
#define ZIP_ERR_FORMAT -11
#define ZIP_ERR_ENTRY  -12
#define ZIP_ERR_CRC    -13
#define ZIP_ERR_PATH   -14
#define ZIP_ERR_WRITE  -15

typedef struct {
    char      name[600];
    long long comp;
    long long uncomp;
    long long lho;            /* where the local header sits */
    unsigned  method;         /* 0 = stored, 8 = deflate; nothing else is accepted */
    unsigned  crc;
    int       is_dir;
} zip_ent_t;

static unsigned zip_crc_tbl[256];
static int zip_crc_ready = 0;

static void zip_crc_init(void) {
    if (zip_crc_ready) return;
    for (unsigned i = 0; i < 256; i++) {
        unsigned c = i;
        for (int k = 0; k < 8; k++) c = (c & 1) ? (0xEDB88320u ^ (c >> 1)) : (c >> 1);
        zip_crc_tbl[i] = c;
    }
    zip_crc_ready = 1;
}

static unsigned zip_crc_up(unsigned crc, const unsigned char *p, size_t n) {
    crc = ~crc;
    while (n--) crc = zip_crc_tbl[(crc ^ *p++) & 0xFF] ^ (crc >> 8);
    return ~crc;
}

/* ITS OWN SHORT WRITE LOOP, rather than the host's. The PS5 build has write_all_checked() and the
   PS4 build does not, and a shared file that needs a different helper from each host is a shared
   file in name only - it would be two files the next time one of them was edited. Six lines is a
   smaller price than that. */
static int zip_write_all(int fd, const unsigned char *p, size_t n) {
    size_t off = 0;
    while (off < n) {
        ssize_t w = write(fd, p + off, n - off);
        if (w <= 0) {
            if (w < 0 && errno == EINTR) continue;
            return -1;
        }
        off += (size_t)w;
    }
    return 0;
}

static unsigned zl16(const unsigned char *p) { return (unsigned)p[0] | ((unsigned)p[1] << 8); }
static unsigned zl32(const unsigned char *p) {
    return (unsigned)p[0] | ((unsigned)p[1] << 8) | ((unsigned)p[2] << 16) | ((unsigned)p[3] << 24);
}
static unsigned long long zl64(const unsigned char *p) {
    return (unsigned long long)zl32(p) | ((unsigned long long)zl32(p + 4) << 32);
}

static int zip_pread(int fd, void *buf, size_t n, long long off) {
    if (lseek(fd, (off_t)off, SEEK_SET) != (off_t)off) return 0;
    size_t got = 0;
    while (got < n) {
        ssize_t r = read(fd, (char *)buf + got, n - got);
        if (r < 0) { if (errno == EINTR) continue; return 0; }
        if (r == 0) return 0;
        got += (size_t)r;
    }
    return 1;
}

/* A name we are willing to create. The archive is somebody else's file and an entry called
   "../../system/x" or "/system/x" is how an unpacker writes outside the folder it was given;
   backslashes are normalised because some writers use them and then "..\\.." would slip past a
   check that only looked for the forward-slash form. */
static int zip_name_ok(char *name) {
    if (!name[0]) return 0;
    for (char *p = name; *p; p++) {
        if (*p == '\\') *p = '/';
        if ((unsigned char)*p < 0x20) return 0;
    }
    if (name[0] == '/') return 0;
    if (name[1] == ':') return 0;                       /* a drive letter */
    if (!strncmp(name, "../", 3)) return 0;
    if (strstr(name, "/../")) return 0;
    size_t n = strlen(name);
    if (n >= 3 && !strcmp(name + n - 3, "/..")) return 0;
    if (!strcmp(name, "..")) return 0;
    return 1;
}

/* Find the end-of-central-directory record and report where the directory is and how many
   entries it holds. The record is at the end but may be followed by a comment, so it is hunted
   backwards through the last 64 KB. */
static int zip_find_dir(int fd, long long fsize, long long *dir_off, long long *dir_count) {
    unsigned char tail[66000];
    long long want = fsize < (long long)sizeof(tail) ? fsize : (long long)sizeof(tail);
    if (want < 22) return ZIP_ERR_FORMAT;
    long long base = fsize - want;
    if (!zip_pread(fd, tail, (size_t)want, base)) return ZIP_ERR_OPEN;
    long long eocd = -1;
    for (long long i = want - 22; i >= 0; i--) {
        if (tail[i] == 0x50 && tail[i + 1] == 0x4B && tail[i + 2] == 0x05 && tail[i + 3] == 0x06) {
            eocd = i;
            break;
        }
    }
    if (eocd < 0) return ZIP_ERR_FORMAT;
    *dir_count = zl16(tail + eocd + 10);
    *dir_off = zl32(tail + eocd + 16);

    /* ZIP64: the 32-bit fields saturate and the real ones are in a second record, found through
       a locator that sits immediately before this one. */
    if (*dir_off == 0xFFFFFFFFLL || *dir_count == 0xFFFFLL) {
        long long loc = eocd - 20;
        if (loc < 0) return ZIP_ERR_FORMAT;
        if (!(tail[loc] == 0x50 && tail[loc + 1] == 0x4B && tail[loc + 2] == 0x06 && tail[loc + 3] == 0x07))
            return ZIP_ERR_FORMAT;
        long long z64 = (long long)zl64(tail + loc + 8);
        unsigned char rec[56];
        if (!zip_pread(fd, rec, sizeof(rec), z64)) return ZIP_ERR_OPEN;
        if (!(rec[0] == 0x50 && rec[1] == 0x4B && rec[2] == 0x06 && rec[3] == 0x06))
            return ZIP_ERR_FORMAT;
        *dir_count = (long long)zl64(rec + 32);
        *dir_off = (long long)zl64(rec + 48);
    }
    if (*dir_off < 0 || *dir_off >= fsize) return ZIP_ERR_FORMAT;
    if (*dir_count < 0 || *dir_count > 200000) return ZIP_ERR_FORMAT;
    return 0;
}

/* Walk the central directory. The callback returns 0 to carry on, non-zero to stop (and that
   value is returned). */
static int zip_walk(int fd, long long fsize, int (*cb)(const zip_ent_t *, void *), void *ctx) {
    long long off = 0, count = 0;
    int rc = zip_find_dir(fd, fsize, &off, &count);
    if (rc) return rc;
    zip_crc_init();
    for (long long i = 0; i < count; i++) {
        unsigned char h[46];
        if (!zip_pread(fd, h, sizeof(h), off)) return ZIP_ERR_FORMAT;
        if (!(h[0] == 0x50 && h[1] == 0x4B && h[2] == 0x01 && h[3] == 0x02)) return ZIP_ERR_FORMAT;
        zip_ent_t e;
        memset(&e, 0, sizeof(e));
        e.method = zl16(h + 10);
        e.crc    = zl32(h + 16);
        e.comp   = (long long)zl32(h + 20);
        e.uncomp = (long long)zl32(h + 24);
        unsigned nlen = zl16(h + 28), elen = zl16(h + 30), clen = zl16(h + 32);
        e.lho = (long long)zl32(h + 42);
        if (nlen == 0 || nlen >= sizeof(e.name)) return ZIP_ERR_FORMAT;
        if (!zip_pread(fd, e.name, nlen, off + 46)) return ZIP_ERR_FORMAT;
        e.name[nlen] = 0;

        /* ZIP64 extra field: whichever of the three saturated, in this order. */
        if (elen) {
            unsigned char ex[1024];
            unsigned take = elen < sizeof(ex) ? elen : (unsigned)sizeof(ex);
            if (zip_pread(fd, ex, take, off + 46 + nlen)) {
                unsigned p = 0;
                while (p + 4 <= take) {
                    unsigned id = zl16(ex + p), sz = zl16(ex + p + 2);
                    if (p + 4 + sz > take) break;
                    if (id == 0x0001) {
                        unsigned q = p + 4;
                        if (e.uncomp == 0xFFFFFFFFLL && q + 8 <= p + 4 + sz) {
                            e.uncomp = (long long)zl64(ex + q); q += 8;
                        }
                        if (e.comp == 0xFFFFFFFFLL && q + 8 <= p + 4 + sz) {
                            e.comp = (long long)zl64(ex + q); q += 8;
                        }
                        if (e.lho == 0xFFFFFFFFLL && q + 8 <= p + 4 + sz) {
                            e.lho = (long long)zl64(ex + q); q += 8;
                        }
                        break;
                    }
                    p += 4 + sz;
                }
            }
        }
        size_t nl = strlen(e.name);
        e.is_dir = (nl && (e.name[nl - 1] == '/' || e.name[nl - 1] == '\\')) ||
                   (e.uncomp == 0 && e.comp == 0 && nl && e.name[nl - 1] == '/');
        rc = cb(&e, ctx);
        if (rc) return rc;
        off += 46 + nlen + elen + clen;
        if (off >= fsize) break;
    }
    return 0;
}

/* Where this entry's bytes begin: past its local header, whose name and extra lengths are the
   ones that count (they are allowed to differ from the central directory's). */
static int zip_data_at(int fd, const zip_ent_t *e, long long *out) {
    unsigned char lh[30];
    if (!zip_pread(fd, lh, sizeof(lh), e->lho)) return ZIP_ERR_ENTRY;
    if (!(lh[0] == 0x50 && lh[1] == 0x4B && lh[2] == 0x03 && lh[3] == 0x04)) return ZIP_ERR_ENTRY;
    *out = e->lho + 30 + (long long)zl16(lh + 26) + (long long)zl16(lh + 28);
    return 0;
}

/* Extract one entry to an already-openable path. The CRC is checked as the bytes go past, so a
   damaged entry is refused rather than left on disk looking finished. */
static int zip_extract_one(int fd, const zip_ent_t *e, const char *dest) {
    if (e->method != 0 && e->method != 8) return ZIP_ERR_ENTRY;
    long long at = 0;
    int rc = zip_data_at(fd, e, &at);
    if (rc) return rc;

    char part[800];
    snprintf(part, sizeof(part), "%s.part", dest);
    mkparents(part);
    unlink(part);
    int fo = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (fo < 0) return ZIP_ERR_WRITE;

    long long wrote = 0;
    unsigned crc = 0;
    if (e->method == 0) {
        /* Stored. Still CRC'd: a stored entry can be damaged too. */
        if (lseek(fd, (off_t)at, SEEK_SET) != (off_t)at) { close(fo); unlink(part); return ZIP_ERR_ENTRY; }
        static unsigned char buf[32768];
        long long left = e->uncomp;
        while (left > 0) {
            size_t want = left < (long long)sizeof(buf) ? (size_t)left : sizeof(buf);
            ssize_t r = read(fd, buf, want);
            if (r <= 0) { if (r < 0 && errno == EINTR) continue; close(fo); unlink(part); return ZIP_ERR_ENTRY; }
            crc = zip_crc_up(crc, buf, (size_t)r);
            if (zip_write_all(fo, buf, (size_t)r) != 0) {
                close(fo); unlink(part); return ZIP_ERR_WRITE;
            }
            wrote += r;
            left -= r;
        }
    } else {
        if (lseek(fd, (off_t)at, SEEK_SET) != (off_t)at) { close(fo); unlink(part); return ZIP_ERR_ENTRY; }
        long long produced = 0;
        int irc = inf_run(fd, e->comp, fo, &produced);
        if (irc != INF_OK) { close(fo); unlink(part); return irc; }
        wrote = produced;
    }
    close(fo);

    if (e->uncomp >= 0 && wrote != e->uncomp) { unlink(part); return ZIP_ERR_ENTRY; }

    /* The deflate path wrote straight through to the file, so the CRC is taken on a read-back.
       That is one extra pass over the bytes and it is worth it: this runs unattended. */
    if (e->method == 8) {
        int fv = open(part, O_RDONLY);
        if (fv < 0) { unlink(part); return ZIP_ERR_WRITE; }
        static unsigned char vb[32768];
        crc = 0;
        for (;;) {
            ssize_t r = read(fv, vb, sizeof(vb));
            if (r < 0) { if (errno == EINTR) continue; close(fv); unlink(part); return ZIP_ERR_ENTRY; }
            if (r == 0) break;
            crc = zip_crc_up(crc, vb, (size_t)r);
        }
        close(fv);
    }
    if (crc != e->crc) { unlink(part); return ZIP_ERR_CRC; }

    unlink(dest);                       /* rename-onto-existing does not replace on this console */
    if (rename(part, dest) != 0) { unlink(part); return ZIP_ERR_WRITE; }
    return 0;
}

#endif /* PMS_ZIP_H */

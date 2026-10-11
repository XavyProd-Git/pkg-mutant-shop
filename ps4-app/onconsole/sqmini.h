/* sqmini - read-only SQLite scan, COPIED VERBATIM from ps5-app/onconsole/server.c.

   It is a copy on purpose. This block is proven on hardware and is pure format work with no
   platform in it, but the PS5 build is shipping and must not be edited to share it: turning it
   into a common header would mean touching server.c, and nothing on the PS4 side is worth that
   risk. If it ever changes on the PS5 side, re-run tools/ps4_sync_sqmini.py rather than editing
   this file by hand - that script checks the two copies still match.

   Extracted from server.c lines 656-904.
 */
#pragma once
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <stdio.h>

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

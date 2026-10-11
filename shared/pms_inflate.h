/* pms_inflate.h - RFC 1951 inflate for both console builds. ONE implementation, included by
 * ps5-app/onconsole/server.c and ps4-app/onconsole/server_ps4.c.
 *
 * It is a header of statics, like cheat_core.h and sqmini.h beside it: each ELF is one
 * translation unit, so there is nothing for a library to buy here. Unlike github_lane.h this is
 * not a generated copy - it was written after both builds existed, so neither had to be left
 * alone, and a copy is a thing that rots.
 *
 * Needs from the host: <string.h>, <unistd.h>, <errno.h>, memcpy, read, write.
 *
 * Tested by tools/test_archive.py: 492 generated streams against Python's own zlib - every block
 * shape, every compression level, sizes on and around each buffer boundary, 400 random - plus six
 * corrupt streams that must be refused rather than crash or hang.
 */
#ifndef PMS_INFLATE_H
#define PMS_INFLATE_H

/* ---------------- opening an archive on the console -----------------------------------------
 *
 * WHY THIS EXISTS. Most of the emulators ship a .zip, and the console had nothing to open one
 * with: neither payload SDK links a decompressor. Both ship a zlib.h - the PS4 at
 * target/include/zlib.h, the PS5 at target/include/sys/zlib.h - and NEITHER ships a library that
 * defines inflate(); checked with nm over every .so and .a in both SDKs, zero hits. A header you
 * cannot link is worse than no header, because it looks like the problem is solved.
 *
 * So the app carries its own, and it is written here rather than vendored for the same reason
 * sqmini.h is: it is a few hundred lines of well-specified byte logic (RFC 1951 and the ZIP
 * appnote), it has to run inside a payload with no allocator worth leaning on, and a copy of
 * somebody else's library would be a copy nobody here understands. It is tested against Python's
 * own zlib over thousands of generated cases and every real release archive this app offers -
 * see tools/test_unzip.py, which is the only reason to trust it.
 *
 * SHAPE. Streaming, both ends: the compressed bytes are read from a file descriptor in 32 KB
 * bites and the output goes straight to another descriptor through the 32 KB window that
 * back-references need anyway. Nothing is held whole in memory, so a 268 MB archive costs the
 * same as a 2 MB one. There is one 32 KB window, one 32 KB input buffer and the two Huffman
 * tables; that is the whole footprint.
 * ------------------------------------------------------------------------------------------- */

/* ---- the bit reader ------------------------------------------------------------------------
 * Deflate is little-endian at the bit level: the first bit of a byte is the least significant.
 * Huffman codes are the exception and are read most-significant-bit first, which is why
 * inf_huff below reverses as it walks. */

#define INF_IN_BYTES   (32 * 1024)
#define INF_WIN_BYTES  (32 * 1024)
#define INF_WIN_MASK   (INF_WIN_BYTES - 1)   /* a power of two, so the wrap is a mask */

/* Errors are negative and distinct, because "the archive did not open" is not an answer anyone
 * can act on. They are mapped to sentences at the route. */
enum {
    INF_OK          =  0,
    INF_ERR_READ    = -1,   /* the source would not read */
    INF_ERR_WRITE   = -2,   /* the destination would not take the bytes (disk full?) */
    INF_ERR_DATA    = -3,   /* the stream is not valid deflate */
    INF_ERR_EOF     = -4,   /* it ended in the middle of something */
    INF_ERR_MEM     = -5
};

/* EITHER END CAN BE A FILE OR A BLOCK OF MEMORY, and the choice is a negative descriptor.
   The archive reader wants file-to-file; the thumbnail maker wants memory-to-memory, because a
   PNG's compressed scanlines arrive in a buffer and have to come back as bytes rather than as a
   file written and read again. Both choices are made ONCE per stream, in inf_refill and
   inf_flush - never on the per-byte path, which is where all the time goes. */
typedef struct {
    int            fd_in;                /* < 0 means read from mem_in */
    const unsigned char *mem_in;
    long long      in_left;              /* compressed bytes we are allowed to read, either way */
    unsigned char  inbuf[INF_IN_BYTES];
    int            inlen, inpos;

    unsigned long  bitbuf;
    int            bitcnt;

    unsigned char  win[INF_WIN_BYTES];
    unsigned       wpos;                 /* next write position in the window */

    int            fd_out;               /* < 0 means write into mem_out */
    unsigned char *mem_out;
    long long      out_cap;              /* how much mem_out holds; ignored for a descriptor */
    long long      produced;
    int            err;
} inf_t;

/* A canonical Huffman table, puff's representation: how many codes of each length, and the
 * symbols in canonical order. Decoding walks the lengths one bit at a time, which is fifteen
 * iterations in the worst case and needs no table building beyond two small arrays. */
typedef struct {
    short count[16];
    short symbol[288];
} inf_huff_t;

static int inf_flush(inf_t *s, unsigned upto) {
    /* Write the window out as far as `upto`, which is where the next byte will go. */
    if (upto == 0) return INF_OK;
    if (s->fd_out < 0) {
        /* INTO MEMORY. `produced` has already counted these bytes - INF_PUT increments it before
           the window fills - so where they belong is produced MINUS upto, not produced. A sink
           that is too small is a refusal, not a truncation: the caller sized it from the PNG
           header and a mismatch means the header and the data disagree. */
        long long at = s->produced - (long long)upto;
        if (!s->mem_out || at < 0 || at + (long long)upto > s->out_cap) return INF_ERR_WRITE;
        memcpy(s->mem_out + at, s->win, upto);
        return INF_OK;
    }
    size_t off = 0;
    while (off < upto) {
        ssize_t w = write(s->fd_out, s->win + off, upto - off);
        if (w <= 0) {
            if (w < 0 && errno == EINTR) continue;
            return INF_ERR_WRITE;
        }
        off += (size_t)w;
    }
    return INF_OK;
}

/* One output byte, through the window, and one bit, as MACROS rather than calls.

   The window IS the output buffer: a back-reference can only reach 32 KB, so the bytes we must
   keep and the bytes we have not written yet are the same bytes.

   These were functions first, and that version was correct and a hundred times too slow - 160 s
   for a 31 MB archive, which on a console is an hour for RetroArch's 752 MB. Almost all of it was
   a call per bit in the symbol decode and a call plus two divides per byte in the match copy. The
   algorithm is unchanged; only the accounting moved inline. */
#define INF_PUT(S, C) do {                                   \
        (S)->win[(S)->wpos++] = (unsigned char)(C);          \
        (S)->produced++;                                     \
        if ((S)->wpos == INF_WIN_BYTES) {                    \
            int _rc = inf_flush((S), INF_WIN_BYTES);         \
            if (_rc != INF_OK) return _rc;                   \
            (S)->wpos = 0;                                   \
        }                                                    \
    } while (0)

/* One bit, inline. The accumulator refills a byte at a time from the input buffer, so the only
   call left on this path is the refill at a buffer boundary. */
#define INF_BIT(S, OUT) do {                                 \
        if ((S)->bitcnt == 0) {                              \
            if ((S)->inpos >= (S)->inlen && !inf_refill(S)) { \
                (S)->err = INF_ERR_EOF;                      \
                return -1;                                   \
            }                                                \
            (S)->bitbuf = (S)->inbuf[(S)->inpos++];          \
            (S)->bitcnt = 8;                                 \
        }                                                    \
        (OUT) = (int)((S)->bitbuf & 1u);                     \
        (S)->bitbuf >>= 1;                                   \
        (S)->bitcnt--;                                       \
    } while (0)

static int inf_refill(inf_t *s) {
    if (s->in_left <= 0) return 0;
    size_t want = INF_IN_BYTES;
    if ((long long)want > s->in_left) want = (size_t)s->in_left;
    if (s->fd_in < 0) {
        /* FROM MEMORY, in the same 32 KB bites. Copied rather than pointed at, so that every
           other line in this file - the bit reader above all - keeps exactly one shape to reason
           about. 32 KB of memcpy per 32 KB of input does not register next to the bit work. */
        if (!s->mem_in) return 0;
        memcpy(s->inbuf, s->mem_in, want);
        s->mem_in += want;
        s->inlen = (int)want;
        s->inpos = 0;
        s->in_left -= (long long)want;
        return 1;
    }
    ssize_t r;
    for (;;) {
        r = read(s->fd_in, s->inbuf, want);
        if (r < 0 && errno == EINTR) continue;
        break;
    }
    if (r <= 0) return 0;
    s->inlen = (int)r;
    s->inpos = 0;
    s->in_left -= r;
    return 1;
}

/* `need` bits, least significant first. Returns -1 at the end of the data. */
static long inf_bits(inf_t *s, int need) {
    long val = (long)s->bitbuf;
    while (s->bitcnt < need) {
        if (s->inpos >= s->inlen && !inf_refill(s)) { s->err = INF_ERR_EOF; return -1; }
        val |= (long)s->inbuf[s->inpos++] << s->bitcnt;
        s->bitcnt += 8;
    }
    s->bitbuf = (unsigned long)(val >> need);
    s->bitcnt -= need;
    return val & ((1L << need) - 1);
}

/* One Huffman symbol. Codes are stored most-significant-bit first, so the accumulator is built
 * up a bit at a time and compared against the first code of each length. */
static int inf_huff(inf_t *s, const inf_huff_t *h) {
    int code = 0, first = 0, index = 0;
    for (int len = 1; len <= 15; len++) {
        int b;
        INF_BIT(s, b);
        code |= b;
        int count = h->count[len];
        if (code - count < first) return h->symbol[index + (code - first)];
        index += count;
        first += count;
        first <<= 1;
        code <<= 1;
    }
    s->err = INF_ERR_DATA;
    return -1;
}

/* Build the canonical table from a list of code lengths. */
static int inf_build(inf_huff_t *h, const short *length, int n) {
    int symbol, len, left;
    short offs[16];
    for (len = 0; len <= 15; len++) h->count[len] = 0;
    for (symbol = 0; symbol < n; symbol++) h->count[length[symbol]]++;
    if (h->count[0] == n) return 0;            /* no codes at all - an empty table is legal */
    /* Over-subscribed is corruption; incomplete is legal only for a single-symbol distance
       table, which the caller allows by ignoring a positive return. */
    left = 1;
    for (len = 1; len <= 15; len++) {
        left <<= 1;
        left -= h->count[len];
        if (left < 0) return left;
    }
    offs[1] = 0;
    for (len = 1; len < 15; len++) offs[len + 1] = offs[len] + h->count[len];
    for (symbol = 0; symbol < n; symbol++)
        if (length[symbol]) h->symbol[offs[length[symbol]]++] = (short)symbol;
    return left;
}

static const short INF_LBASE[29] = {
    3, 4, 5, 6, 7, 8, 9, 10, 11, 13, 15, 17, 19, 23, 27, 31,
    35, 43, 51, 59, 67, 83, 99, 115, 131, 163, 195, 227, 258 };
static const short INF_LEXT[29] = {
    0, 0, 0, 0, 0, 0, 0, 0, 1, 1, 1, 1, 2, 2, 2, 2,
    3, 3, 3, 3, 4, 4, 4, 4, 5, 5, 5, 5, 0 };
static const short INF_DBASE[30] = {
    1, 2, 3, 4, 5, 7, 9, 13, 17, 25, 33, 49, 65, 97, 129, 193,
    257, 385, 513, 769, 1025, 1537, 2049, 3073, 4097, 6145, 8193, 12289, 16385, 24577 };
static const short INF_DEXT[30] = {
    0, 0, 0, 0, 1, 1, 2, 2, 3, 3, 4, 4, 5, 5, 6, 6,
    7, 7, 8, 8, 9, 9, 10, 10, 11, 11, 12, 12, 13, 13 };

/* One compressed block's worth of literals and matches. */
static int inf_codes(inf_t *s, const inf_huff_t *lencode, const inf_huff_t *distcode) {
    for (;;) {
        int symbol = inf_huff(s, lencode);
        if (symbol < 0) return s->err ? s->err : INF_ERR_DATA;
        if (symbol < 256) {
            INF_PUT(s, symbol);
            continue;
        }
        if (symbol == 256) return INF_OK;                 /* end of block */
        symbol -= 257;
        if (symbol >= 29) return INF_ERR_DATA;
        long extra = inf_bits(s, INF_LEXT[symbol]);
        if (extra < 0) return INF_ERR_EOF;
        int len = INF_LBASE[symbol] + (int)extra;

        symbol = inf_huff(s, distcode);
        if (symbol < 0) return s->err ? s->err : INF_ERR_DATA;
        if (symbol >= 30) return INF_ERR_DATA;
        extra = inf_bits(s, INF_DEXT[symbol]);
        if (extra < 0) return INF_ERR_EOF;
        unsigned dist = (unsigned)(INF_DBASE[symbol] + (int)extra);
        if ((long long)dist > s->produced) return INF_ERR_DATA;   /* before the start */

        /* Copy from the window. The ranges legitimately overlap - that is how deflate encodes
           a run - so this stays a forward byte copy rather than a memcpy, but the wrap is a mask
           and the window write is inline: a call and two divides per byte was most of the cost of
           the whole decoder. */
        unsigned from = (s->wpos - dist) & INF_WIN_MASK;
        while (len--) {
            unsigned char c = s->win[from];
            from = (from + 1) & INF_WIN_MASK;
            INF_PUT(s, c);
        }
    }
}

static int inf_stored(inf_t *s) {
    s->bitbuf = 0;
    s->bitcnt = 0;                                   /* a stored block is byte-aligned */
    unsigned char hdr[4];
    for (int i = 0; i < 4; i++) {
        if (s->inpos >= s->inlen && !inf_refill(s)) return INF_ERR_EOF;
        hdr[i] = s->inbuf[s->inpos++];
    }
    unsigned len = (unsigned)hdr[0] | ((unsigned)hdr[1] << 8);
    unsigned nlen = (unsigned)hdr[2] | ((unsigned)hdr[3] << 8);
    if ((len ^ 0xFFFFu) != nlen) return INF_ERR_DATA;
    while (len) {
        if (s->inpos >= s->inlen && !inf_refill(s)) return INF_ERR_EOF;
        /* A stored block is a straight copy, so take it in runs: whatever is left in the input
           buffer and whatever room is left in the window, whichever is smaller. */
        unsigned have = (unsigned)(s->inlen - s->inpos);
        unsigned room = INF_WIN_BYTES - s->wpos;
        unsigned take = len < have ? len : have;
        if (take > room) take = room;
        memcpy(s->win + s->wpos, s->inbuf + s->inpos, take);
        s->inpos += (int)take;
        s->wpos += take;
        s->produced += (long long)take;
        len -= take;
        if (s->wpos == INF_WIN_BYTES) {
            int rc = inf_flush(s, INF_WIN_BYTES);
            if (rc != INF_OK) return rc;
            s->wpos = 0;
        }
    }
    return INF_OK;
}

static int inf_fixed(inf_t *s) {
    static inf_huff_t lencode, distcode;
    static int ready = 0;
    if (!ready) {
        short lengths[288];
        int symbol;
        for (symbol = 0; symbol < 144; symbol++) lengths[symbol] = 8;
        for (; symbol < 256; symbol++) lengths[symbol] = 9;
        for (; symbol < 280; symbol++) lengths[symbol] = 7;
        for (; symbol < 288; symbol++) lengths[symbol] = 8;
        inf_build(&lencode, lengths, 288);
        for (symbol = 0; symbol < 30; symbol++) lengths[symbol] = 5;
        inf_build(&distcode, lengths, 30);
        ready = 1;
    }
    return inf_codes(s, &lencode, &distcode);
}

static int inf_dynamic(inf_t *s) {
    static const short ORDER[19] = { 16, 17, 18, 0, 8, 7, 9, 6, 10, 5, 11, 4, 12, 3, 13, 2, 14, 1, 15 };
    short lengths[288 + 30];
    inf_huff_t lencode, distcode;

    long nlen = inf_bits(s, 5);
    long ndist = inf_bits(s, 5);
    long ncode = inf_bits(s, 4);
    if (nlen < 0 || ndist < 0 || ncode < 0) return INF_ERR_EOF;
    nlen += 257; ndist += 1; ncode += 4;
    if (nlen > 286 || ndist > 30) return INF_ERR_DATA;

    int index;
    for (index = 0; index < ncode; index++) {
        long b = inf_bits(s, 3);
        if (b < 0) return INF_ERR_EOF;
        lengths[ORDER[index]] = (short)b;
    }
    for (; index < 19; index++) lengths[ORDER[index]] = 0;
    if (inf_build(&lencode, lengths, 19) != 0) return INF_ERR_DATA;

    index = 0;
    while (index < nlen + ndist) {
        int symbol = inf_huff(s, &lencode);
        if (symbol < 0) return s->err ? s->err : INF_ERR_DATA;
        if (symbol < 16) {
            lengths[index++] = (short)symbol;
            continue;
        }
        int len = 0;
        long extra;
        if (symbol == 16) {
            if (index == 0) return INF_ERR_DATA;
            len = lengths[index - 1];
            extra = inf_bits(s, 2);
            if (extra < 0) return INF_ERR_EOF;
            symbol = 3 + (int)extra;
        } else if (symbol == 17) {
            extra = inf_bits(s, 3);
            if (extra < 0) return INF_ERR_EOF;
            symbol = 3 + (int)extra;
        } else {
            extra = inf_bits(s, 7);
            if (extra < 0) return INF_ERR_EOF;
            symbol = 11 + (int)extra;
        }
        if (index + symbol > nlen + ndist) return INF_ERR_DATA;
        while (symbol--) lengths[index++] = (short)len;
    }
    if (lengths[256] == 0) return INF_ERR_DATA;          /* no end-of-block code */

    int err = inf_build(&lencode, lengths, (int)nlen);
    if (err && (err < 0 || nlen != lencode.count[0] + lencode.count[1])) return INF_ERR_DATA;
    err = inf_build(&distcode, lengths + nlen, (int)ndist);
    if (err && (err < 0 || ndist != distcode.count[0] + distcode.count[1])) return INF_ERR_DATA;
    return inf_codes(s, &lencode, &distcode);
}

/* Inflate `comp_len` bytes of raw deflate from fd_in (already positioned) into fd_out.
   Returns INF_OK and sets *out_len, or a negative INF_ERR_*. */
static int inf_run_x(int fd_in, const unsigned char *mem_in, long long comp_len,
                     int fd_out, unsigned char *mem_out, long long out_cap,
                     long long *out_len) {
    static inf_t s;                     /* 64 KB of buffers - too big for a payload's stack */
    memset(&s, 0, sizeof(s));
    s.fd_in = fd_in;
    s.mem_in = mem_in;
    s.in_left = comp_len;
    s.fd_out = fd_out;
    s.mem_out = mem_out;
    s.out_cap = out_cap;

    int rc = INF_OK;
    for (;;) {
        long last = inf_bits(&s, 1);
        if (last < 0) { rc = INF_ERR_EOF; break; }
        long type = inf_bits(&s, 2);
        if (type < 0) { rc = INF_ERR_EOF; break; }
        if (type == 0)      rc = inf_stored(&s);
        else if (type == 1) rc = inf_fixed(&s);
        else if (type == 2) rc = inf_dynamic(&s);
        else                rc = INF_ERR_DATA;
        if (rc != INF_OK) break;
        if (last) break;
    }
    if (rc == INF_OK) rc = inf_flush(&s, s.wpos);
    if (out_len) *out_len = s.produced;
    return rc;
}

/* The two shapes anyone actually asks for. */
static int inf_run(int fd_in, long long comp_len, int fd_out, long long *out_len) {
    return inf_run_x(fd_in, NULL, comp_len, fd_out, NULL, 0, out_len);
}

/* Memory to memory. `comp_len` is how much of `src` is deflate; out_cap is what `dst` holds.
   NOT reentrant and deliberately so - inf_run_x keeps its 64 KB static, one stream at a time -
   which is what both callers do: one archive member, or one PNG. */
static int inf_mem(const unsigned char *src, long long comp_len,
                   unsigned char *dst, long long out_cap, long long *out_len) {
    return inf_run_x(-1, src, comp_len, -1, dst, out_cap, out_len);
}

#endif /* PMS_INFLATE_H */

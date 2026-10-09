/* pms_thumb.h - CARD-SIZED COVER ART, MADE ON THE CONSOLE, FROM THE GAME'S OWN FILES.
 *
 * WHY THIS EXISTS. The library's covers are each title's icon0.png out of /user/appmeta, and they
 * are 512 px originals: measured on the owner's consoles, 81 of them weigh 25.2 MB on the PS5 and
 * 23 of them weigh 7.1 MB on the PS4, mean 327 KB each. That is what a page open cost whenever no
 * PC was running, because the only thing that had ever made a small copy was the companion - and
 * the one console whose browser least tolerates it, the PS4, had no cache at all and served the
 * full-size PNG for every card. The same covers through this file are ~31 KB: 2.1 MB a library
 * instead of 25.
 *
 * It needed two things the SDKs do not have. The decompressor is now in pms_inflate.h - that is
 * what made this possible at all, a PNG being deflate with a filter in front of it - and the JPEG
 * encoder is here, because there is no image library in either SDK either (and the console's other
 * copy of the art is a BC7-compressed DDS, which is worse to read than the PNG).
 *
 * NOTHING LEAVES THE CONSOLE. No PC, no network, no cache to seed: the source is a file in the
 * title's own folder and the answer is written next to the ones a companion would have written,
 * so send_thumb needs no new branch and a console whose cache a PC filled behaves as before.
 *
 * WHAT IT DOES NOT DO, deliberately:
 *   interlaced PNG (Adam7)  - refused. No icon on either console is interlaced, and the caller
 *                             already has a correct answer for "could not make one": serve the
 *                             original, which is what it did for every title until now.
 *   progressive JPEG        - baseline only. Every browser reads baseline; the PS5's does.
 *   chroma subsampling      - 4:4:4. At 320 px the saving is a few KB and the cost is a second
 *                             sampling grid to get wrong.
 *
 * Needs from the host: pms_inflate.h, <stdlib.h>, <string.h>, <fcntl.h>, <unistd.h>,
 *                      <sys/stat.h>, mkparents().
 *
 * Tested by tools/test_thumb.py: every icon0.png on both consoles is decoded here and by Pillow
 * and the pixels compared, and every JPEG this writes is read back by Pillow and compared against
 * what Pillow itself would have produced from the same pixels.
 */
#ifndef PMS_THUMB_H
#define PMS_THUMB_H

/* 320 px, the same as the companion's THUMB_PX: about 1.5x the largest card, so it stays crisp on
   a 4K TV and still costs a tenth of the original. Quality 85 rather than the companion's 90 for
   one reason - this one runs on the console, where the bytes also have to cross the LAN to a
   phone sometimes, and 85 to 90 is 20% more file for a difference nobody has ever pointed at. */
#define THUMB_PX       320
#define THUMB_QUALITY  85

/* A ceiling on what will be read at all. The largest icon on either console is 816,547 bytes and
   the largest plausible one is 1024x1024; 24 MB of decoded pixels is the real cost and this bounds
   it. A payload that mallocs without a bound is a payload that takes the console down. */
#define THUMB_MAX_PNG   (12 * 1024 * 1024)
#define THUMB_MAX_DIM   2048

enum {
    THUMB_OK          =  0,
    THUMB_ERR_OPEN    = -1,    /* no such file, or it would not read */
    THUMB_ERR_SIG     = -2,    /* not a PNG */
    THUMB_ERR_SHAPE   = -3,    /* a PNG, but not one this can decode (interlaced, 1/2/4-bit) */
    THUMB_ERR_DATA    = -4,    /* the compressed scanlines are not valid */
    THUMB_ERR_MEM     = -5,
    THUMB_ERR_WRITE   = -6
};

/* ---- reading the file ---------------------------------------------------------------------- */

/* The whole file, or nothing. Caller frees. */
static unsigned char *thumb_slurp(const char *path, long *out_n) {
    struct stat st;
    if (stat(path, &st) != 0 || !S_ISREG(st.st_mode)) return NULL;
    if (st.st_size <= 0 || (long long)st.st_size > THUMB_MAX_PNG) return NULL;
    int fd = open(path, O_RDONLY);
    if (fd < 0) return NULL;
    long n = (long)st.st_size;
    unsigned char *b = (unsigned char *)malloc((size_t)n);
    if (!b) { close(fd); return NULL; }
    long got = 0;
    while (got < n) {
        ssize_t r = read(fd, b + got, (size_t)(n - got));
        if (r < 0) { if (errno == EINTR) continue; break; }
        if (r == 0) break;
        got += r;
    }
    close(fd);
    if (got != n) { free(b); return NULL; }
    if (out_n) *out_n = n;
    return b;
}

static unsigned thumb_be32(const unsigned char *p) {
    return ((unsigned)p[0] << 24) | ((unsigned)p[1] << 16) | ((unsigned)p[2] << 8) | p[3];
}

/* ---- the PNG --------------------------------------------------------------------------------
 * A PNG is a signature, then chunks of {length, type, data, crc}. What matters here is IHDR (the
 * shape), PLTE (a palette, if the colour type says so), and IDAT - which may be SPLIT ACROSS ANY
 * NUMBER OF CHUNKS and has to be joined before it can be inflated, because a deflate stream does
 * not care where the container broke it. */

typedef struct {
    int w, h;
    int colour;             /* 0 gray, 2 RGB, 3 palette, 4 gray+alpha, 6 RGBA */
    int depth;              /* bits per sample: 8 or 16 here */
    int interlace;
    unsigned char pal[256 * 3];
    int npal;
    unsigned char *rgb;     /* w*h*3, caller frees */
} png_t;

/* How many bytes one pixel occupies in the RAW (post-filter) scanlines. */
static int png_bpp(int colour, int depth) {
    int ch;
    switch (colour) {
        case 0: ch = 1; break;
        case 2: ch = 3; break;
        case 3: ch = 1; break;
        case 4: ch = 2; break;
        case 6: ch = 4; break;
        default: return 0;
    }
    return ch * (depth / 8);
}

/* The PNG filters, undone in place, one scanline at a time. `bpp` is the pixel stride in bytes -
 * the spec's `bpp` for filter purposes, which is bytes per pixel and at least 1. `prev` is the
 * already-unfiltered line above, or NULL for the first. */
static void png_unfilter(int type, unsigned char *cur, const unsigned char *prev,
                         long len, int bpp) {
    long i;
    switch (type) {
        case 0:                                        /* None */
            break;
        case 1:                                        /* Sub */
            for (i = bpp; i < len; i++) cur[i] = (unsigned char)(cur[i] + cur[i - bpp]);
            break;
        case 2:                                        /* Up */
            if (prev) for (i = 0; i < len; i++) cur[i] = (unsigned char)(cur[i] + prev[i]);
            break;
        case 3:                                        /* Average */
            for (i = 0; i < len; i++) {
                int a = (i >= bpp) ? cur[i - bpp] : 0;
                int b = prev ? prev[i] : 0;
                cur[i] = (unsigned char)(cur[i] + ((a + b) >> 1));
            }
            break;
        case 4:                                        /* Paeth */
            for (i = 0; i < len; i++) {
                int a = (i >= bpp) ? cur[i - bpp] : 0;
                int b = prev ? prev[i] : 0;
                int c = (prev && i >= bpp) ? prev[i - bpp] : 0;
                int p = a + b - c;
                int pa = p > a ? p - a : a - p;
                int pb = p > b ? p - b : b - p;
                int pc = p > c ? p - c : c - p;
                int pr = (pa <= pb && pa <= pc) ? a : (pb <= pc ? b : c);
                cur[i] = (unsigned char)(cur[i] + pr);
            }
            break;
        default:
            break;                                     /* an unknown filter leaves the line alone */
    }
}

/* Decode `src` into p->rgb. Alpha is composited onto WHITE rather than discarded: these are
 * opaque rectangles in practice, so it changes nothing for a real icon, and for one that does
 * carry transparency a white card is a sane picture where raw RGB under a zero alpha is noise. */
static int png_decode(const unsigned char *src, long n, png_t *p) {
    static const unsigned char SIG[8] = { 0x89, 'P', 'N', 'G', '\r', '\n', 0x1A, '\n' };
    memset(p, 0, sizeof(*p));
    if (n < 8 + 25 || memcmp(src, SIG, 8) != 0) return THUMB_ERR_SIG;

    /* Pass one: the header, the palette, and how much IDAT there is in total. */
    long at = 8, idat_total = 0;
    int have_ihdr = 0;
    while (at + 8 <= n) {
        unsigned len = thumb_be32(src + at);
        const unsigned char *type = src + at + 4;
        if (len > (unsigned)(n - at - 8)) break;               /* truncated: stop, do not guess */
        const unsigned char *data = src + at + 8;
        if (!memcmp(type, "IHDR", 4)) {
            if (len < 13) return THUMB_ERR_SHAPE;
            p->w = (int)thumb_be32(data);
            p->h = (int)thumb_be32(data + 4);
            p->depth = data[8];
            p->colour = data[9];
            p->interlace = data[12];
            have_ihdr = 1;
            if (p->w <= 0 || p->h <= 0 || p->w > THUMB_MAX_DIM || p->h > THUMB_MAX_DIM)
                return THUMB_ERR_SHAPE;
            /* 1, 2 and 4-bit images would need a bit unpacker as well, and no icon on either
               console is one. Interlaced would need the seven Adam7 passes. Both are refused by
               name rather than decoded wrongly. */
            if (p->depth != 8 && p->depth != 16) return THUMB_ERR_SHAPE;
            if (p->interlace != 0) return THUMB_ERR_SHAPE;
            if (!png_bpp(p->colour, p->depth)) return THUMB_ERR_SHAPE;
        } else if (!memcmp(type, "PLTE", 4)) {
            p->npal = (int)(len / 3);
            if (p->npal > 256) p->npal = 256;
            memcpy(p->pal, data, (size_t)p->npal * 3);
        } else if (!memcmp(type, "IDAT", 4)) {
            idat_total += (long)len;
        } else if (!memcmp(type, "IEND", 4)) {
            break;
        }
        at += 8 + (long)len + 4;
    }
    if (!have_ihdr || idat_total < 3) return THUMB_ERR_SHAPE;
    if (p->colour == 3 && p->npal <= 0) return THUMB_ERR_SHAPE;

    /* Pass two: join the IDATs. The zlib wrapper's two header bytes are skipped - and checked,
       because a byte that is not a zlib header means this is not the stream we think it is. */
    unsigned char *z = (unsigned char *)malloc((size_t)idat_total);
    if (!z) return THUMB_ERR_MEM;
    long zn = 0;
    at = 8;
    while (at + 8 <= n) {
        unsigned len = thumb_be32(src + at);
        const unsigned char *type = src + at + 4;
        if (len > (unsigned)(n - at - 8)) break;
        if (!memcmp(type, "IDAT", 4)) {
            memcpy(z + zn, src + at + 8, len);
            zn += (long)len;
        } else if (!memcmp(type, "IEND", 4)) {
            break;
        }
        at += 8 + (long)len + 4;
    }
    if (zn < 3 || (z[0] & 0x0F) != 8) { free(z); return THUMB_ERR_DATA; }

    int bpp = png_bpp(p->colour, p->depth);
    long stride = 1 + (long)p->w * bpp;                /* the filter byte, then the pixels */
    long raw_n = stride * (long)p->h;
    unsigned char *raw = (unsigned char *)malloc((size_t)raw_n);
    if (!raw) { free(z); return THUMB_ERR_MEM; }

    long long got = 0;
    int rc = inf_mem(z + 2, zn - 2, raw, raw_n, &got);
    free(z);
    /* SHORT IS NOT FATAL, AND THIS IS DELIBERATE. A PNG whose last chunk is clipped still has
       every line before the clip, and half a cover is a better card than initials. Anything that
       did not even produce one line is a refusal. */
    if (rc != INF_OK && got < stride) { free(raw); return THUMB_ERR_DATA; }
    long lines = got / stride;
    if (lines <= 0) { free(raw); return THUMB_ERR_DATA; }
    if (lines > p->h) lines = p->h;

    /* Unfilter, then flatten to RGB. The filter's pixel stride is bytes per pixel, minimum 1. */
    int fbpp = bpp < 1 ? 1 : bpp;
    for (long y = 0; y < lines; y++) {
        unsigned char *cur = raw + y * stride;
        png_unfilter(cur[0], cur + 1, y ? (raw + (y - 1) * stride + 1) : NULL,
                     stride - 1, fbpp);
    }

    p->rgb = (unsigned char *)malloc((size_t)p->w * (size_t)p->h * 3);
    if (!p->rgb) { free(raw); return THUMB_ERR_MEM; }
    memset(p->rgb, 0xFF, (size_t)p->w * (size_t)p->h * 3);     /* clipped lines read as white */
    int step = p->depth / 8;                                   /* 16-bit takes the high byte */
    for (long y = 0; y < lines; y++) {
        const unsigned char *s = raw + y * stride + 1;
        unsigned char *d = p->rgb + y * (long)p->w * 3;
        for (int x = 0; x < p->w; x++) {
            const unsigned char *px = s + (long)x * bpp;
            int r, g, b, a = 255;
            switch (p->colour) {
                case 0:  r = g = b = px[0]; break;
                case 2:  r = px[0]; g = px[step]; b = px[2 * step]; break;
                case 3: {
                    int idx = px[0];
                    if (idx >= p->npal) idx = 0;
                    r = p->pal[idx * 3]; g = p->pal[idx * 3 + 1]; b = p->pal[idx * 3 + 2];
                    break;
                }
                case 4:  r = g = b = px[0]; a = px[step]; break;
                default: r = px[0]; g = px[step]; b = px[2 * step]; a = px[3 * step]; break;
            }
            if (a != 255) {                        /* onto white */
                r = (r * a + 255 * (255 - a) + 127) / 255;
                g = (g * a + 255 * (255 - a) + 127) / 255;
                b = (b * a + 255 * (255 - a) + 127) / 255;
            }
            d[x * 3] = (unsigned char)r;
            d[x * 3 + 1] = (unsigned char)g;
            d[x * 3 + 2] = (unsigned char)b;
        }
    }
    free(raw);
    return THUMB_OK;
}

/* ---- the resize -----------------------------------------------------------------------------
 * An AREA AVERAGE: every destination pixel is the mean of the source rectangle it covers, with
 * each source pixel weighted by how much of it falls inside - which is what Pillow calls BOX and
 * every other library calls area resampling.
 *
 * THE WEIGHTS ARE THE POINT, and the first version of this did without them. 512 px down to 320
 * is a ratio of 1.6, so a destination pixel covers one source pixel and three fifths of the next;
 * taking whole pixels and dividing by the count instead of weighting them scored a mean error of
 * up to 11.5 out of 255 against Pillow on the owner's own covers - visible aliasing on anything
 * with fine detail, which cover art is full of. With the weights it is under 1.
 *
 * It never ENLARGES: a cover smaller than the target is left alone, because stretching it adds
 * bytes and no detail. */
static unsigned char *thumb_resize(const unsigned char *src, int sw, int sh,
                                   int dw, int dh) {
    unsigned char *dst = (unsigned char *)malloc((size_t)dw * (size_t)dh * 3);
    if (!dst) return NULL;
    double xs = (double)sw / (double)dw, ys = (double)sh / (double)dh;
    for (int dy = 0; dy < dh; dy++) {
        double sy0 = dy * ys, sy1 = sy0 + ys;
        long y0 = (long)sy0, y1 = (long)(sy1 + 0.9999999);
        if (y1 > sh) y1 = sh;
        for (int dx = 0; dx < dw; dx++) {
            double sx0 = dx * xs, sx1 = sx0 + xs;
            long x0 = (long)sx0, x1 = (long)(sx1 + 0.9999999);
            if (x1 > sw) x1 = sw;
            double r = 0, g = 0, b = 0, tot = 0;
            for (long y = y0; y < y1; y++) {
                /* How much of source row y lies inside this destination row. */
                double wy = (sy1 < (double)(y + 1) ? sy1 : (double)(y + 1))
                          - (sy0 > (double)y ? sy0 : (double)y);
                if (wy <= 0) continue;
                const unsigned char *row = src + y * (long)sw * 3;
                for (long x = x0; x < x1; x++) {
                    double wx = (sx1 < (double)(x + 1) ? sx1 : (double)(x + 1))
                              - (sx0 > (double)x ? sx0 : (double)x);
                    if (wx <= 0) continue;
                    double w = wx * wy;
                    r += row[x * 3] * w;
                    g += row[x * 3 + 1] * w;
                    b += row[x * 3 + 2] * w;
                    tot += w;
                }
            }
            unsigned char *d = dst + ((long)dy * dw + dx) * 3;
            if (tot <= 0) { d[0] = d[1] = d[2] = 0xFF; continue; }
            int ri = (int)(r / tot + 0.5), gi = (int)(g / tot + 0.5), bi = (int)(b / tot + 0.5);
            d[0] = (unsigned char)(ri < 0 ? 0 : (ri > 255 ? 255 : ri));
            d[1] = (unsigned char)(gi < 0 ? 0 : (gi > 255 ? 255 : gi));
            d[2] = (unsigned char)(bi < 0 ? 0 : (bi > 255 ? 255 : bi));
        }
    }
    return dst;
}

/* ---- the JPEG encoder -----------------------------------------------------------------------
 * Baseline sequential, 8-bit, three components at 1x1, the standard tables from Annex K of the
 * JPEG specification. Writing those tables rather than deriving our own is the point: a decoder
 * has to be handed the tables anyway, and the standard ones are what every encoder in the world
 * emits, so there is nothing here for a reader to disagree with. */

static const int JQ_LUMA[64] = {
    16, 11, 10, 16, 24, 40, 51, 61,      12, 12, 14, 19, 26, 58, 60, 55,
    14, 13, 16, 24, 40, 57, 69, 56,      14, 17, 22, 29, 51, 87, 80, 62,
    18, 22, 37, 56, 68,109,103, 77,      24, 35, 55, 64, 81,104,113, 92,
    49, 64, 78, 87,103,121,120,101,      72, 92, 95, 98,112,100,103, 99
};
static const int JQ_CHROMA[64] = {
    17, 18, 24, 47, 99, 99, 99, 99,      18, 21, 26, 66, 99, 99, 99, 99,
    24, 26, 56, 99, 99, 99, 99, 99,      47, 66, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,      99, 99, 99, 99, 99, 99, 99, 99,
    99, 99, 99, 99, 99, 99, 99, 99,      99, 99, 99, 99, 99, 99, 99, 99
};
static const int JZIG[64] = {
     0,  1,  8, 16,  9,  2,  3, 10,     17, 24, 32, 25, 18, 11,  4,  5,
    12, 19, 26, 33, 40, 48, 41, 34,     27, 20, 13,  6,  7, 14, 21, 28,
    35, 42, 49, 56, 57, 50, 43, 36,     29, 22, 15, 23, 30, 37, 44, 51,
    58, 59, 52, 45, 38, 31, 39, 46,     53, 60, 61, 54, 47, 55, 62, 63
};

/* The standard Huffman tables: the count of codes of each length, then the symbols. */
static const unsigned char JDC_L_BITS[17] = { 0, 0,1,5,1,1,1,1,1,1,0,0,0,0,0,0,0 };
static const unsigned char JDC_L_VAL[12]  = { 0,1,2,3,4,5,6,7,8,9,10,11 };
static const unsigned char JDC_C_BITS[17] = { 0, 0,3,1,1,1,1,1,1,1,1,1,0,0,0,0,0 };
static const unsigned char JDC_C_VAL[12]  = { 0,1,2,3,4,5,6,7,8,9,10,11 };
static const unsigned char JAC_L_BITS[17] = { 0, 0,2,1,3,3,2,4,3,5,5,4,4,0,0,1,0x7d };
static const unsigned char JAC_L_VAL[162] = {
    0x01,0x02,0x03,0x00,0x04,0x11,0x05,0x12,0x21,0x31,0x41,0x06,0x13,0x51,0x61,0x07,
    0x22,0x71,0x14,0x32,0x81,0x91,0xa1,0x08,0x23,0x42,0xb1,0xc1,0x15,0x52,0xd1,0xf0,
    0x24,0x33,0x62,0x72,0x82,0x09,0x0a,0x16,0x17,0x18,0x19,0x1a,0x25,0x26,0x27,0x28,
    0x29,0x2a,0x34,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,0x46,0x47,0x48,0x49,
    0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,0x69,
    0x6a,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7a,0x83,0x84,0x85,0x86,0x87,0x88,0x89,
    0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,0xa4,0xa5,0xa6,0xa7,
    0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,0xc4,0xc5,
    0xc6,0xc7,0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,0xe1,0xe2,
    0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf1,0xf2,0xf3,0xf4,0xf5,0xf6,0xf7,0xf8,
    0xf9,0xfa
};
static const unsigned char JAC_C_BITS[17] = { 0, 0,2,1,2,4,4,3,4,7,5,4,4,0,1,2,0x77 };
static const unsigned char JAC_C_VAL[162] = {
    0x00,0x01,0x02,0x03,0x11,0x04,0x05,0x21,0x31,0x06,0x12,0x41,0x51,0x07,0x61,0x71,
    0x13,0x22,0x32,0x81,0x08,0x14,0x42,0x91,0xa1,0xb1,0xc1,0x09,0x23,0x33,0x52,0xf0,
    0x15,0x62,0x72,0xd1,0x0a,0x16,0x24,0x34,0xe1,0x25,0xf1,0x17,0x18,0x19,0x1a,0x26,
    0x27,0x28,0x29,0x2a,0x35,0x36,0x37,0x38,0x39,0x3a,0x43,0x44,0x45,0x46,0x47,0x48,
    0x49,0x4a,0x53,0x54,0x55,0x56,0x57,0x58,0x59,0x5a,0x63,0x64,0x65,0x66,0x67,0x68,
    0x69,0x6a,0x73,0x74,0x75,0x76,0x77,0x78,0x79,0x7a,0x82,0x83,0x84,0x85,0x86,0x87,
    0x88,0x89,0x8a,0x92,0x93,0x94,0x95,0x96,0x97,0x98,0x99,0x9a,0xa2,0xa3,0xa4,0xa5,
    0xa6,0xa7,0xa8,0xa9,0xaa,0xb2,0xb3,0xb4,0xb5,0xb6,0xb7,0xb8,0xb9,0xba,0xc2,0xc3,
    0xc4,0xc5,0xc6,0xc7,0xc8,0xc9,0xca,0xd2,0xd3,0xd4,0xd5,0xd6,0xd7,0xd8,0xd9,0xda,
    0xe2,0xe3,0xe4,0xe5,0xe6,0xe7,0xe8,0xe9,0xea,0xf2,0xf3,0xf4,0xf5,0xf6,0xf7,0xf8,
    0xf9,0xfa
};

/* A code per symbol, derived from the canonical lengths - the same derivation a decoder does. */
typedef struct { unsigned short code[256]; unsigned char len[256]; } jhuff_t;

static void jhuff_build(jhuff_t *h, const unsigned char *bits, const unsigned char *vals) {
    memset(h, 0, sizeof(*h));
    unsigned code = 0;
    int k = 0;
    for (int l = 1; l <= 16; l++) {
        for (int i = 0; i < bits[l]; i++) {
            h->code[vals[k]] = (unsigned short)code;
            h->len[vals[k]] = (unsigned char)l;
            code++;
            k++;
        }
        code <<= 1;
    }
}

typedef struct {
    int fd;
    unsigned char buf[8192];
    int n;
    unsigned long bitbuf;
    int bitcnt;
    int err;
} jout_t;

static void jput(jout_t *o, int byte) {
    if (o->n >= (int)sizeof(o->buf)) {
        size_t off = 0;
        while (off < (size_t)o->n) {
            ssize_t w = write(o->fd, o->buf + off, (size_t)o->n - off);
            if (w <= 0) { if (w < 0 && errno == EINTR) continue; o->err = THUMB_ERR_WRITE; o->n = 0; return; }
            off += (size_t)w;
        }
        o->n = 0;
    }
    o->buf[o->n++] = (unsigned char)byte;
}

static void jflush(jout_t *o) {
    size_t off = 0;
    while (off < (size_t)o->n) {
        ssize_t w = write(o->fd, o->buf + off, (size_t)o->n - off);
        if (w <= 0) { if (w < 0 && errno == EINTR) continue; o->err = THUMB_ERR_WRITE; break; }
        off += (size_t)w;
    }
    o->n = 0;
}

static void jput16(jout_t *o, int v) { jput(o, (v >> 8) & 0xFF); jput(o, v & 0xFF); }

/* Entropy-coded bits are most-significant-first, and 0xFF has to be followed by 0x00 so that it
   cannot be read as a marker. Getting that stuffing wrong produces a file that opens in some
   decoders and not others, which is the worst possible outcome. */
static void jbits(jout_t *o, unsigned code, int len) {
    o->bitbuf = (o->bitbuf << len) | (code & ((1u << len) - 1u));
    o->bitcnt += len;
    while (o->bitcnt >= 8) {
        int b = (int)((o->bitbuf >> (o->bitcnt - 8)) & 0xFF);
        jput(o, b);
        if (b == 0xFF) jput(o, 0x00);
        o->bitcnt -= 8;
    }
}

static void jbits_pad(jout_t *o) {
    while (o->bitcnt > 0) jbits(o, 1, 1);               /* 1-bits, as the spec requires */
}

/* The DCT basis, as a table of constants rather than a call to cos().
 *
 * NO LIBM, DELIBERATELY. The PS5 build links kernel_sys, Notification, UserService,
 * SystemService, AppInstUtil, Pad, Ssl and Http, and the PS4's list is shorter still; neither
 * links a maths library, and an undefined cos() is a link error at the end of a four-minute
 * build - or worse, a symbol that resolves to something unexpected. These are
 * cos((2x+1) u pi / 16) with the 1/sqrt2 for u = 0 folded in, printed to nine places, which is
 * past what a float holds. tools/test_thumb.py is what says they are the right numbers: a wrong
 * one would show up immediately as a cover Pillow and this file disagree about. */
static const float JDCT_C[8][8] = {
    { +0.353553391f, +0.353553391f, +0.353553391f, +0.353553391f, +0.353553391f, +0.353553391f, +0.353553391f, +0.353553391f },
    { +0.490392640f, +0.415734806f, +0.277785117f, +0.097545161f, -0.097545161f, -0.277785117f, -0.415734806f, -0.490392640f },
    { +0.461939766f, +0.191341716f, -0.191341716f, -0.461939766f, -0.461939766f, -0.191341716f, +0.191341716f, +0.461939766f },
    { +0.415734806f, -0.097545161f, -0.490392640f, -0.277785117f, +0.277785117f, +0.490392640f, +0.097545161f, -0.415734806f },
    { +0.353553391f, -0.353553391f, -0.353553391f, +0.353553391f, +0.353553391f, -0.353553391f, -0.353553391f, +0.353553391f },
    { +0.277785117f, -0.490392640f, +0.097545161f, +0.415734806f, -0.415734806f, -0.097545161f, +0.490392640f, -0.277785117f },
    { +0.191341716f, -0.461939766f, +0.461939766f, -0.191341716f, -0.191341716f, +0.461939766f, -0.461939766f, +0.191341716f },
    { +0.097545161f, -0.277785117f, +0.415734806f, -0.490392640f, +0.490392640f, -0.415734806f, +0.277785117f, -0.097545161f },
};

/* The forward DCT, separable and written out plainly. 4800 blocks for a 320x320 cover is 5 M
   multiplies, which does not register beside the inflate that produced the pixels. */
static void jfdct(const float *in, float *out) {
    const float (*C)[8] = JDCT_C;
    float tmp[64];
    for (int y = 0; y < 8; y++)
        for (int u = 0; u < 8; u++) {
            float s = 0;
            for (int x = 0; x < 8; x++) s += in[y * 8 + x] * C[u][x];
            tmp[y * 8 + u] = s;
        }
    for (int u = 0; u < 8; u++)
        for (int v = 0; v < 8; v++) {
            float s = 0;
            for (int y = 0; y < 8; y++) s += tmp[y * 8 + u] * C[v][y];
            out[v * 8 + u] = s;
        }
}

/* How many bits a coefficient needs, and the value written in them. */
static int jcat(int v) {
    int a = v < 0 ? -v : v, n = 0;
    while (a) { n++; a >>= 1; }
    return n;
}

/* One 8x8 block: DCT, quantise, then DC as a difference from the previous block of the same
   component and AC as runs of zeros. Returns the new DC predictor. */
static int jblock(jout_t *o, const float *pix, const int *qt,
                  const jhuff_t *hdc, const jhuff_t *hac, int pred) {
    float f[64];
    int q[64];
    jfdct(pix, f);
    for (int i = 0; i < 64; i++) {
        float v = f[i] / (float)qt[i];
        q[i] = (int)(v < 0 ? -(int)(-v + 0.5f) : (int)(v + 0.5f));
    }
    int dc = q[0], diff = dc - pred;
    int s = jcat(diff);
    jbits(o, hdc->code[s], hdc->len[s]);
    if (s) jbits(o, (unsigned)(diff < 0 ? diff - 1 : diff), s);

    int run = 0;
    for (int k = 1; k < 64; k++) {
        int v = q[JZIG[k]];
        if (!v) { run++; continue; }
        while (run > 15) { jbits(o, hac->code[0xF0], hac->len[0xF0]); run -= 16; }
        int sz = jcat(v), sym = (run << 4) | sz;
        jbits(o, hac->code[sym], hac->len[sym]);
        jbits(o, (unsigned)(v < 0 ? v - 1 : v), sz);
        run = 0;
    }
    if (run) jbits(o, hac->code[0x00], hac->len[0x00]);   /* end of block */
    return dc;
}

static void jput_table(jout_t *o, int id, const unsigned char *bits, const unsigned char *vals) {
    int nv = 0;
    for (int l = 1; l <= 16; l++) nv += bits[l];
    jput16(o, 0xFFC4);
    jput16(o, 2 + 1 + 16 + nv);
    jput(o, id);
    for (int l = 1; l <= 16; l++) jput(o, bits[l]);
    for (int i = 0; i < nv; i++) jput(o, vals[i]);
}

/* Write `rgb` (w by h) as a baseline JPEG to an already-open descriptor. */
static int jpeg_write(int fd, const unsigned char *rgb, int w, int h, int quality) {
    static jout_t o;
    static jhuff_t hdcl, hdcc, hacl, hacc;
    static int ql[64], qc[64];
    memset(&o, 0, sizeof(o));
    o.fd = fd;
    jhuff_build(&hdcl, JDC_L_BITS, JDC_L_VAL);
    jhuff_build(&hdcc, JDC_C_BITS, JDC_C_VAL);
    jhuff_build(&hacl, JAC_L_BITS, JAC_L_VAL);
    jhuff_build(&hacc, JAC_C_BITS, JAC_C_VAL);

    /* The standard quality scaling: 50 is the tables as written, above that they shrink. */
    if (quality < 1) quality = 1;
    if (quality > 100) quality = 100;
    int scale = (quality < 50) ? (5000 / quality) : (200 - quality * 2);
    for (int i = 0; i < 64; i++) {
        int a = (JQ_LUMA[i] * scale + 50) / 100;
        int b = (JQ_CHROMA[i] * scale + 50) / 100;
        ql[i] = a < 1 ? 1 : (a > 255 ? 255 : a);
        qc[i] = b < 1 ? 1 : (b > 255 ? 255 : b);
    }

    jput16(&o, 0xFFD8);                                  /* SOI */
    /* JFIF, so that a reader with no other clue knows the component order and the units. */
    jput16(&o, 0xFFE0); jput16(&o, 16);
    jput(&o, 'J'); jput(&o, 'F'); jput(&o, 'I'); jput(&o, 'F'); jput(&o, 0);
    jput(&o, 1); jput(&o, 1); jput(&o, 0);
    jput16(&o, 1); jput16(&o, 1); jput(&o, 0); jput(&o, 0);

    jput16(&o, 0xFFDB); jput16(&o, 2 + 1 + 64); jput(&o, 0);
    for (int i = 0; i < 64; i++) jput(&o, ql[JZIG[i]]);
    jput16(&o, 0xFFDB); jput16(&o, 2 + 1 + 64); jput(&o, 1);
    for (int i = 0; i < 64; i++) jput(&o, qc[JZIG[i]]);

    jput16(&o, 0xFFC0); jput16(&o, 8 + 3 * 3);           /* SOF0, baseline */
    jput(&o, 8);
    jput16(&o, h); jput16(&o, w);
    jput(&o, 3);
    jput(&o, 1); jput(&o, 0x11); jput(&o, 0);            /* Y,  1x1, quant 0 */
    jput(&o, 2); jput(&o, 0x11); jput(&o, 1);            /* Cb, 1x1, quant 1 */
    jput(&o, 3); jput(&o, 0x11); jput(&o, 1);            /* Cr, 1x1, quant 1 */

    jput_table(&o, 0x00, JDC_L_BITS, JDC_L_VAL);
    jput_table(&o, 0x10, JAC_L_BITS, JAC_L_VAL);
    jput_table(&o, 0x01, JDC_C_BITS, JDC_C_VAL);
    jput_table(&o, 0x11, JAC_C_BITS, JAC_C_VAL);

    jput16(&o, 0xFFDA); jput16(&o, 6 + 2 * 3);           /* SOS */
    jput(&o, 3);
    jput(&o, 1); jput(&o, 0x00);
    jput(&o, 2); jput(&o, 0x11);
    jput(&o, 3); jput(&o, 0x11);
    jput(&o, 0); jput(&o, 63); jput(&o, 0);

    int pdc[3] = { 0, 0, 0 };
    float by[64], bcb[64], bcr[64];
    for (int my = 0; my < h; my += 8) {
        for (int mx = 0; mx < w; mx += 8) {
            for (int y = 0; y < 8; y++) {
                /* EDGE BLOCKS REPEAT THE LAST ROW AND COLUMN rather than reading past the image.
                   A cover is rarely a multiple of eight, and padding with black would put a dark
                   fringe down two sides of every card. */
                int sy = my + y; if (sy >= h) sy = h - 1;
                for (int x = 0; x < 8; x++) {
                    int sx = mx + x; if (sx >= w) sx = w - 1;
                    const unsigned char *p = rgb + ((long)sy * w + sx) * 3;
                    float r = p[0], g = p[1], b = p[2];
                    by[y * 8 + x]  =  0.299f * r + 0.587f * g + 0.114f * b - 128.0f;
                    bcb[y * 8 + x] = -0.168736f * r - 0.331264f * g + 0.5f * b;
                    bcr[y * 8 + x] =  0.5f * r - 0.418688f * g - 0.081312f * b;
                }
            }
            pdc[0] = jblock(&o, by,  ql, &hdcl, &hacl, pdc[0]);
            pdc[1] = jblock(&o, bcb, qc, &hdcc, &hacc, pdc[1]);
            pdc[2] = jblock(&o, bcr, qc, &hdcc, &hacc, pdc[2]);
            if (o.err) return o.err;
        }
    }
    jbits_pad(&o);
    jput16(&o, 0xFFD9);                                  /* EOI */
    jflush(&o);
    return o.err ? o.err : THUMB_OK;
}

/* ---- the whole job --------------------------------------------------------------------------- */

/* Make `dst` (a .jpg) from `src` (a .png). THUMB_OK, or a negative code the caller can name.
 *
 * WRITTEN TO A .part AND RENAMED. Two readers want this file the moment it exists - the page, and
 * ShadowMountPlus if it is ever pointed at one of these folders - and half a JPEG is a broken card
 * that stays broken until the grid is rebuilt. rename() on these consoles will not replace an
 * existing file, so the target is removed first; see the note in the archive extractor. */
static int thumb_make(const char *src, const char *dst, int maxpx, int quality) {
    long n = 0;
    unsigned char *file = thumb_slurp(src, &n);
    if (!file) return THUMB_ERR_OPEN;
    png_t p;
    int rc = png_decode(file, n, &p);
    free(file);
    if (rc != THUMB_OK) { free(p.rgb); return rc; }

    int dw = p.w, dh = p.h;
    if (maxpx > 0 && (p.w > maxpx || p.h > maxpx)) {
        if (p.w >= p.h) { dw = maxpx; dh = (int)((long)p.h * maxpx / p.w); }
        else            { dh = maxpx; dw = (int)((long)p.w * maxpx / p.h); }
        if (dw < 1) dw = 1;
        if (dh < 1) dh = 1;
    }
    unsigned char *small = NULL;
    const unsigned char *use = p.rgb;
    if (dw != p.w || dh != p.h) {
        small = thumb_resize(p.rgb, p.w, p.h, dw, dh);
        if (!small) { free(p.rgb); return THUMB_ERR_MEM; }
        use = small;
    }

    char part[1024];
    snprintf(part, sizeof(part), "%s.part", dst);
    mkparents(part);
    unlink(part);
    int fd = open(part, O_WRONLY | O_CREAT | O_TRUNC, 0777);
    if (fd < 0) { free(small); free(p.rgb); return THUMB_ERR_WRITE; }
    rc = jpeg_write(fd, use, dw, dh, quality);
    close(fd);
    free(small);
    free(p.rgb);
    if (rc != THUMB_OK) { unlink(part); return rc; }
    unlink(dst);
    if (rename(part, dst) != 0) { unlink(part); return THUMB_ERR_WRITE; }
    return THUMB_OK;
}

#endif /* PMS_THUMB_H */

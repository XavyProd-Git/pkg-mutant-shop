/* Drives shared/pms_thumb.h on the PC so it can be compared against Pillow.
 *
 * Two modes, because the two halves fail differently:
 *   rgb   <in.png> <out.raw>          the decoder alone - writes "w h\n" then w*h*3 bytes
 *   small <in.png> <out.raw> [px]     decode AND resize, in the same format
 *   jpg   <in.png> <out.jpg> [px] [q] the whole job, exactly as the console runs it
 *
 * `small` exists so the encoder can be judged on its own. Comparing our JPEG against a thumbnail
 * Pillow resized itself measures two things at once - our box filter takes whole source pixels and
 * Pillow's weights them fractionally - and the first run of this did exactly that, charging the
 * encoder for a difference the resize had made.
 *
 * The functions are the shipped ones; nothing here is a second implementation. See
 * tools/test_thumb.py, which is the only reason to trust any of it.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>

#define SHOP_DATA_DIR "/tmp/pms-thumb-harness"

static void mkparents(const char *path) {
    char tmp[1024];
    snprintf(tmp, sizeof(tmp), "%s", path);
    for (char *p = tmp + 1; *p; p++) {
        if (*p != '/') continue;
        *p = 0;
        mkdir(tmp, 0777);
        *p = '/';
    }
}

#include "../shared/pms_inflate.h"
#include "../shared/pms_thumb.h"

int main(int argc, char **argv) {
    if (argc < 4) {
        fprintf(stderr, "usage: thumb_harness rgb|jpg <in.png> <out> [maxpx] [quality]\n");
        return 2;
    }
    if (!strcmp(argv[1], "rgb") || !strcmp(argv[1], "small")) {
        long n = 0;
        unsigned char *f = thumb_slurp(argv[2], &n);
        if (!f) { fprintf(stderr, "rc=%d\n", THUMB_ERR_OPEN); return 1; }
        png_t p;
        int rc = png_decode(f, n, &p);
        free(f);
        if (rc != THUMB_OK) { free(p.rgb); fprintf(stderr, "rc=%d\n", rc); return 1; }
        int w = p.w, h = p.h;
        unsigned char *use = p.rgb, *small = NULL;
        if (!strcmp(argv[1], "small")) {
            int maxpx = argc > 4 ? atoi(argv[4]) : THUMB_PX;
            if (maxpx > 0 && (p.w > maxpx || p.h > maxpx)) {
                if (p.w >= p.h) { w = maxpx; h = (int)((long)p.h * maxpx / p.w); }
                else            { h = maxpx; w = (int)((long)p.w * maxpx / p.h); }
                if (w < 1) w = 1;
                if (h < 1) h = 1;
                small = thumb_resize(p.rgb, p.w, p.h, w, h);
                if (!small) { free(p.rgb); fprintf(stderr, "rc=%d\n", THUMB_ERR_MEM); return 1; }
                use = small;
            }
        }
        FILE *o = fopen(argv[3], "wb");
        if (!o) { free(small); free(p.rgb); return 1; }
        fprintf(o, "%d %d\n", w, h);
        fwrite(use, 1, (size_t)w * (size_t)h * 3, o);
        fclose(o);
        free(small);
        free(p.rgb);
        printf("%d %d colour=%d depth=%d\n", w, h, p.colour, p.depth);
        return 0;
    }
    int px = argc > 4 ? atoi(argv[4]) : THUMB_PX;
    int q  = argc > 5 ? atoi(argv[5]) : THUMB_QUALITY;
    int rc = thumb_make(argv[2], argv[3], px, q);
    if (rc != THUMB_OK) { fprintf(stderr, "rc=%d\n", rc); return 1; }
    struct stat st;
    stat(argv[3], &st);
    printf("%lld\n", (long long)st.st_size);
    return 0;
}

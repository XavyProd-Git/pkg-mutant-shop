# -*- coding: utf-8 -*-
"""shared/pms_thumb.h against independent implementations, over every real icon on both consoles.

WHY THIS TEST EXISTS. The console now makes its own cover art - PNG in, JPEG out, no PC anywhere -
and both halves of that are code this project wrote, because neither SDK has an image library. A
decoder that is subtly wrong does not crash; it draws a cover with the channels swapped or a grey
smear down one side, and nobody notices until the owner does.

So it is checked against something that is not ours, ONE STAGE AT A TIME. The first version of
this measured the finished JPEG against a thumbnail Pillow had resized itself, which charged the
encoder for a difference the resize had made, and reported 76 failures in an encoder that was fine.

  1 decode   every icon decoded here and by Pillow, pixels compared EXACTLY. Unfiltering a PNG is
             integer arithmetic with one right answer, and a single wrong Paeth predictor shows up
             as a handful of bytes out of a million. Alpha is the one deliberate difference - this
             composites onto white where Pillow's convert("RGB") discards it - so the comparison is
             made against an explicit composite.
  2 resize   against a SECOND AREA AVERAGE, written here in numpy, to within rounding.
             Deliberately not against Pillow's BOX: that filter gives each source pixel a weight of
             1 or 0 depending on whether its CENTRE falls inside the destination pixel, where this
             - and every other library's "area" - weights it by how much of it overlaps. On a 1.6x
             reduction that is the difference between hard and smooth edges, the overlap version is
             the better picture, and measuring one against the other says nothing about whether
             ours is right. The gap to Pillow is printed as information.
  3 encode   OUR resized pixels are handed to Pillow's encoder at the same quality and 4:4:4, and
             both JPEGs are measured against those same pixels. Now the only difference is the
             encoder. Reading our file at all is itself the test that the markers and the bit
             stuffing are right, because Pillow refuses a malformed one.

    python tools/test_thumb.py                  # the icons under the session's scratchpad
    python tools/test_thumb.py <dir-of-pngs>

Needs Pillow and numpy on the PC. The CONSOLE needs nothing, which is the point of the file under
test.
"""
import glob
import io
import os
import subprocess
import sys

import numpy as np

HERE = os.path.dirname(os.path.abspath(__file__))

RESIZE_MAX_OFF = 1        # per channel, against the numpy area average: ties round either way
ENCODE_SLACK = 0.75       # ours, minus Pillow's, on the same pixels


def wsl(p):
    return p.replace("C:\\", "/mnt/c/").replace("C:/", "/mnt/c/").replace("\\", "/")


def sh(args, timeout=600):
    return subprocess.run(["wsl", "-e"] + args, capture_output=True, text=True,
                          timeout=timeout, errors="replace")


def build(out):
    # NO -lm, AND THAT IS PART OF THE TEST. Neither console SDK links a maths library, so a cos()
    # that crept into this file would build here and fail to link there - four minutes later,
    # inside a build script. Compiling the harness the same way proves the dependency is not there.
    # -Wno-unused-function because these are headers of statics and each console build uses a
    # different subset; an unused one here says nothing about either build.
    r = sh(["bash", "-lc", "gcc -O2 -Wall -Wextra -Wno-unused-function -o '%s' '%s'"
            % (wsl(out), wsl(os.path.join(HERE, "thumb_harness.c")))])
    if r.returncode != 0:
        print(r.stdout + r.stderr)
        sys.exit("the harness did not compile")
    for w in [l for l in (r.stderr or "").splitlines() if "warning" in l][:10]:
        print("  warn:", w)


def pillow_rgb(path):
    """What Pillow decodes, composited onto white the same way pms_thumb.h does."""
    from PIL import Image
    im = Image.open(path)
    im.load()
    if im.mode in ("RGBA", "LA", "PA") or "transparency" in im.info:
        im = im.convert("RGBA")
        bg = Image.new("RGBA", im.size, (255, 255, 255, 255))
        im = Image.alpha_composite(bg, im).convert("RGB")
    else:
        im = im.convert("RGB")
    return im


def read_raw(path):
    with io.open(path, "rb") as f:
        hdr = b""
        while not hdr.endswith(b"\n"):
            c = f.read(1)
            if not c:
                return 0, 0, b""
            hdr += c
        w, h = (int(x) for x in hdr.split())
        return w, h, f.read()


def mae(a, b):
    x = np.frombuffer(bytes(a), dtype=np.uint8).astype(np.float64)
    y = np.frombuffer(bytes(b), dtype=np.uint8).astype(np.float64)
    return float(np.abs(x - y).mean())


def _weights(src, dst):
    """One row per destination pixel: how much of each source pixel it covers, normalised.

    The rule shared/pms_thumb.h implements, restated. A destination pixel spans
    [d*src/dst, (d+1)*src/dst) of the source and each source pixel contributes the length of the
    overlap. Written as a matrix so the whole resize is two multiplies.
    """
    sc = src / float(dst)
    w = np.zeros((dst, src), dtype=np.float64)
    lo = np.arange(src, dtype=np.float64)
    hi = lo + 1.0
    for d in range(dst):
        a, b = d * sc, (d + 1) * sc
        w[d] = np.clip(np.minimum(hi, b) - np.maximum(lo, a), 0.0, None)
    tot = w.sum(axis=1, keepdims=True)
    tot[tot == 0] = 1.0
    return w / tot


def area_average(im, dw, dh):
    """The reference resize: an exact area average, independent of both Pillow and the C."""
    if (dw, dh) == (im.width, im.height):
        return np.asarray(im, dtype=np.uint8).reshape(-1)
    a = np.asarray(im, dtype=np.float64)                       # h, w, 3
    wy, wx = _weights(im.height, dh), _weights(im.width, dw)
    out = np.einsum("yh,hwc,xw->yxc", wy, a, wx, optimize=True)
    return np.clip(np.floor(out + 0.5), 0, 255).astype(np.uint8).reshape(-1)


def target_size(w, h, maxpx=320):
    """The size pms_thumb.h computes: integer truncation, and never an enlargement."""
    if w <= maxpx and h <= maxpx:
        return (w, h)
    if w >= h:
        return (maxpx, max(1, h * maxpx // w))
    return (max(1, w * maxpx // h), maxpx)


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else os.path.join(
        os.environ.get("CLAUDE_SCRATCHPAD", ""), "icons")
    pngs = sorted(glob.glob(os.path.join(src, "*.png")))
    if not pngs:
        print("no PNGs in %s - nothing to compare against, so nothing is claimed" % src)
        return 0
    try:
        from PIL import Image
    except ImportError:
        print("Pillow is not installed on this PC, so this test cannot run - and a test that")
        print("cannot run must not report a pass.")
        return 1

    work = os.path.join(os.environ.get("TEMP", "/tmp"), "pms-thumb-test")
    os.makedirs(work, exist_ok=True)
    harness = os.path.join(work, "thumb_harness")
    build(harness)

    bad = 0
    shaped = {}
    worst = {"resize": 0, "encode": 0.0, "pilbox": 0.0}
    bytes_mine = bytes_pil = bytes_png = 0
    done = 0
    for p in pngs:
        name = os.path.basename(p)

        # ---- 1. the decoder, exactly
        raw = os.path.join(work, "a.raw")
        r = sh([wsl(harness), "rgb", wsl(p), wsl(raw)])
        if r.returncode != 0:
            rc = (r.stderr or "").strip()
            try:
                info = Image.open(p)
                mode, inter = info.mode, info.info.get("interlace", 0)
                pillow_rgb(p)
            except Exception:
                k = "unreadable by Pillow either"
                shaped[k] = shaped.get(k, 0) + 1
                continue
            if inter or mode in ("1", "P;1", "P;2", "P;4"):
                k = "refused, and Pillow agrees the shape is an odd one"
                shaped[k] = shaped.get(k, 0) + 1
                continue
            print("  %-28s REFUSED (%s) but Pillow decoded it as %s" % (name, rc, mode))
            bad += 1
            continue
        bytes_png += os.path.getsize(p)
        w, h, px = read_raw(raw)
        im = pillow_rgb(p)
        if (w, h) != (im.width, im.height):
            print("  %-28s SHAPE %dx%d vs Pillow %dx%d" % (name, w, h, im.width, im.height))
            bad += 1
            continue
        want = im.tobytes()
        if px != want:
            a = np.frombuffer(px, dtype=np.uint8).astype(np.int16)
            b = np.frombuffer(want, dtype=np.uint8).astype(np.int16)
            print("  %-28s DECODE %d of %d bytes differ, worst by %d"
                  % (name, int((a != b).sum()), len(want), int(np.abs(a - b).max())))
            bad += 1
            continue

        # ---- 2. the resize, against an independent area average
        sraw = os.path.join(work, "s.raw")
        r = sh([wsl(harness), "small", wsl(p), wsl(sraw), "320"])
        if r.returncode != 0:
            print("  %-28s RESIZE failed (%s)" % (name, (r.stderr or "").strip()))
            bad += 1
            continue
        sw, sh_, spx = read_raw(sraw)
        exp = target_size(im.width, im.height)
        if (sw, sh_) != exp:
            print("  %-28s RESIZE %dx%d, expected %dx%d" % (name, sw, sh_, exp[0], exp[1]))
            bad += 1
            continue
        ref = area_average(im, sw, sh_)
        off = int(np.abs(np.frombuffer(spx, dtype=np.uint8).astype(np.int16)
                         - ref.astype(np.int16)).max())
        worst["resize"] = max(worst["resize"], off)
        if exp != (im.width, im.height):
            worst["pilbox"] = max(worst["pilbox"],
                                  mae(spx, im.resize(exp, Image.BOX).tobytes()))
        if off > RESIZE_MAX_OFF:
            print("  %-28s RESIZE up to %d off the area average" % (name, off))
            bad += 1
            continue

        # ---- 3. the encoder, on our own pixels, against Pillow's encoder on the same pixels
        mine = os.path.join(work, "mine.jpg")
        r = sh([wsl(harness), "jpg", wsl(p), wsl(mine), "320", "85"])
        if r.returncode != 0:
            print("  %-28s ENCODE failed (%s)" % (name, (r.stderr or "").strip()))
            bad += 1
            continue
        ours = Image.frombytes("RGB", (sw, sh_), spx)
        theirs = os.path.join(work, "theirs.jpg")
        # subsampling=0 is 4:4:4, which is what pms_thumb.h writes. libjpeg's default at this
        # quality is 4:2:0, and comparing against that would be comparing two formats.
        ours.save(theirs, "JPEG", quality=85, subsampling=0)
        try:
            got = Image.open(mine)
            got.load()
        except Exception as ex:
            print("  %-28s ENCODE produced a file Pillow will not read: %r" % (name, ex))
            bad += 1
            continue
        if got.size != (sw, sh_):
            print("  %-28s ENCODE size %r, expected %r" % (name, got.size, (sw, sh_)))
            bad += 1
            continue
        m_mine = mae(got.convert("RGB").tobytes(), spx)
        ref_jpg = Image.open(theirs)
        ref_jpg.load()
        m_pil = mae(ref_jpg.convert("RGB").tobytes(), spx)
        worst["encode"] = max(worst["encode"], m_mine - m_pil)
        bytes_mine += os.path.getsize(mine)
        bytes_pil += os.path.getsize(theirs)
        if m_mine > m_pil + ENCODE_SLACK:
            print("  %-28s ENCODE mean error %.2f against Pillow's %.2f on the same pixels"
                  % (name, m_mine, m_pil))
            bad += 1
            continue
        done += 1

    print()
    for k, v in sorted(shaped.items()):
        print("  %d x %s" % (v, k))
    print("  decode: exact on every icon that was decoded")
    print("  resize: worst %d off the area average (allowed %d); Pillow's own BOX, a different"
          % (worst["resize"], RESIZE_MAX_OFF))
    print("          filter, differs from ours by %.2f on these covers" % worst["pilbox"])
    print("  encode: worst %.2f WORSE than Pillow on the same pixels (allowed %.2f)"
          % (worst["encode"], ENCODE_SLACK))
    if bytes_pil:
        print("  %d cover(s): %.2f MB of PNG in, %.2f MB of JPEG out (Pillow's would be %.2f)"
              % (done, bytes_png / 1048576.0, bytes_mine / 1048576.0, bytes_pil / 1048576.0))
    print("%d icon(s) compared, %d failure(s)" % (len(pngs), bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())

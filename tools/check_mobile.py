# -*- coding: utf-8 -*-
"""Gate: every direct child of the header has an explicit order in the phone layout.

WHY THIS EXISTS. The page's header is a WRAPPING FLEXBOX whose children are re-sequenced on a phone
with `order`. Two things about that are easy to get wrong and neither shows up anywhere else:

  * An item with no `order` defaults to 0, which sorts BEFORE `order:1`. So forgetting one does not
    leave it where it was - it throws it to the FRONT. Measured 2026-10-01 on a 375px phone: `.qcol`
    (the install queue and the Payloads & Homebrews button) and `#consoleSel` (the console picker)
    had no order, so both jumped above the logo. The header was 319px of an 812px screen, in five
    ragged rows, and 56% of the screen was gone before the first game.
  * `order` only applies to FLEX ITEMS - direct children. `.dock{order:3}` named the install queue,
    which is a GRANDchild (it lives inside `.qcol`), so the rule did nothing at all and had been
    dead since `.qcol` was introduced. The comment above it still described a row it could not
    produce.

Both are invisible in review: the CSS looks complete, every selector exists, nothing errors. Only
the rendered order is wrong. So this reads the markup, lists the header's real direct children, and
refuses any that the phone block does not place.

    python tools/check_mobile.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
PAGE = os.path.join(ROOT, "web", "index.html")

# The block that owns the phone layout. Named by its opening line so a reshuffle is loud.
PHONE_AT = "@media (max-width:640px){"

fails = []
n = 0


def ok(cond, what, detail=""):
    global n
    n += 1
    if not cond:
        fails.append("%s%s" % (what, (" - " + detail) if detail else ""))


def header_children(src):
    """The direct children of <header>, as the selectors you would style them with.

    A tag at a time, not a line at a time: several of the header's children open on one line and
    close many lines later, and an element's attributes routinely wrap, so counting angle brackets
    per line put the depth out immediately and found exactly one child out of eleven. A gate that
    inspects one element reports OK for a header that is entirely unplaced.
    """
    m = re.search(r"\n<header>(.*?)\n</header>", src, re.S)
    if not m:
        return None
    body = re.sub(r"<!--.*?-->", "", m.group(1), flags=re.S)   # comments open nothing
    void = {"img", "input", "br", "hr", "meta", "link", "source", "path", "circle", "rect"}
    kids, depth = [], 0
    for tok in re.finditer(r"<(/?)([a-zA-Z0-9]+)([^>]*?)(/?)>", body, re.S):
        closing, tag, attrs, selfclose = tok.group(1), tok.group(2).lower(), tok.group(3), tok.group(4)
        if closing:
            depth -= 1
            continue
        if depth == 0:
            ident = re.search(r'id="([^"]+)"', attrs)
            cls = re.search(r'class="([^"]+)"', attrs)
            kids.append(("#" + ident.group(1)) if ident
                        else (("." + cls.group(1).split()[0]) if cls else tag))
        if not selfclose and tag not in void:
            depth += 1
    return kids


def phone_block(src):
    """EVERY block with this condition, joined - there is more than one.

    The page has two `@media (max-width:640px)` blocks: an earlier one for the sub-bar and the
    drives, and the later one that owns the header's order. Reading only the first found a 13-line
    block, reported that nothing was ordered and that the scroll fades did not exist - a gate that
    fails for its own reasons teaches people to ignore it.
    """
    out, at = [], 0
    while True:
        i = src.find(PHONE_AT, at)
        if i < 0:
            break
        depth, j = 0, i + len(PHONE_AT) - 1
        while j < len(src):
            if src[j] == "{":
                depth += 1
            elif src[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        out.append(src[i:j + 1])
        at = j + 1
    return (chr(10)).join(out) if out else None


def main():
    src = io.open(PAGE, encoding="utf-8").read()
    kids = header_children(src)
    ok(bool(kids), "the header's children can be read from the markup")
    blk = phone_block(src)
    ok(bool(blk), "the phone layout block is where this expects it", PHONE_AT)
    if not kids or not blk:
        print("check_mobile: FAIL")
        for f in fails:
            print("   %s" % f)
        return 1

    # `.spacer` is deliberately display:none on a phone, so it needs no order.
    hidden = set(re.findall(r"\n\s*([.#][\w-]+)\s*\{[^}]*display:\s*none", blk))
    missing = []
    for sel in kids:
        if sel in hidden:
            continue
        # order may be set on the selector alone or in a grouped rule.
        # ANY rule for this selector may carry the order - a selector is usually styled more than
        # once in the same block, and only one of them sets it.
        if not any("order:" in body for body in
                   re.findall(re.escape(sel) + r"\s*\{([^}]*)\}", blk)):
            missing.append(sel)
    ok(not missing,
       "every header child is placed by the phone layout (an unplaced one jumps to the FRONT)",
       ", ".join(missing))

    # An order on something that is not a direct child does nothing - the trap that hid for weeks.
    ordered = set(re.findall(r"\n\s*([.#][\w-]+)\s*\{[^}]*order:\s*-?\d", blk))
    header_only = {s for s in ordered if s in ("#dock", ".dock", ".dockhead")}
    ok(not header_only,
       "no order is set on something that is not a header child",
       ", ".join(sorted(header_only)))

    # The sideways scrollers have to say they scroll, or content is simply unreachable: measured at
    # 375px, four of the five drive tiles were off the edge with nothing to suggest it.
    # A GRADIENT, specifically - `mask-image:none` in the .atend rule is also "mask-image", and
    # checking for the word alone passed with the fade deleted.
    faded = re.search(r"mask-image:\s*linear-gradient", blk) is not None
    ok(faded, "the sideways scrollers fade at the edge, so it is clear there is more")
    for sel in (".drives", ".segrow"):
        ok(re.search(re.escape(sel) + r"[^{]*\{[^}]*(mask-image|overflow-x)", blk) is not None,
           "%s is set up as a sideways scroller" % sel)
    ok(re.search(r"\.atend[^{]*\{[^}]*mask-image:\s*none", blk) is not None,
       "the fade is removed once there is nothing more to scroll to")

    if fails:
        print("check_mobile: FAIL")
        for f in fails:
            print("   %s" % f)
        return 1
    print("check_mobile: OK (%d checks, %d header children all placed)" % (n, len(kids)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

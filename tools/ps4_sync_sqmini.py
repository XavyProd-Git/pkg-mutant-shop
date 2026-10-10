# -*- coding: utf-8 -*-
"""Keep ps4-app/onconsole/sqmini.h honest against its source in ps5-app/onconsole/server.c.

The PS4 payload needs the same read-only SQLite scan the PS5 payload uses. That block is pure
format work with no platform in it, but the PS5 file is shipping and is not going to be edited to
share it - the risk is all downside. So it is COPIED, and this script is what stops a copy from
quietly rotting: it re-extracts the block and compares.

    python tools/ps4_sync_sqmini.py            # update the copy from server.c
    python tools/ps4_sync_sqmini.py --check    # exit 1 if it has drifted (used by the PS4 build)
"""
import io
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "ps5-app", "onconsole", "server.c")
DST = os.path.join(ROOT, "ps4-app", "onconsole", "sqmini.h")


def extract():
    lines = io.open(SRC, encoding="utf-8", errors="replace").read().split("\n")
    start = None
    for i, l in enumerate(lines):
        if "sqmini" in l and "read-only" in l:
            j = i
            while j > 0 and not lines[j].startswith("/* ---"):
                j -= 1
            start = j
            break
    if start is None:
        raise SystemExit("could not find the sqmini block in %s" % SRC)
    end = None
    for i, l in enumerate(lines):
        if l.startswith("static int sq_col_index"):
            j = i
            while j < len(lines) and lines[j].rstrip() != "}":
                j += 1
            end = j
            break
    if end is None:
        raise SystemExit("could not find the end of sq_col_index in %s" % SRC)
    header = (
        "/* sqmini - read-only SQLite scan, COPIED VERBATIM from ps5-app/onconsole/server.c.\n"
        "\n"
        "   It is a copy on purpose. This block is proven on hardware and is pure format work with no\n"
        "   platform in it, but the PS5 build is shipping and must not be edited to share it: turning it\n"
        "   into a common header would mean touching server.c, and nothing on the PS4 side is worth that\n"
        "   risk. If it ever changes on the PS5 side, re-run tools/ps4_sync_sqmini.py rather than editing\n"
        "   this file by hand - that script checks the two copies still match.\n"
        "\n"
        "   Extracted from server.c lines %d-%d.\n"
        " */\n"
        "#pragma once\n"
        "#include <stdint.h>\n"
        "#include <stdlib.h>\n"
        "#include <string.h>\n"
        "#include <stdio.h>\n\n" % (start + 1, end + 1)
    )
    return header + "\n".join(lines[start:end + 1]) + "\n"


def body_only(text):
    """Compare the code, not the header comment (whose line numbers legitimately move)."""
    i = text.find("/* ---------------------------------------------------------------------------")
    return text[i:] if i >= 0 else text


def main():
    want = extract()
    check = "--check" in sys.argv
    have = io.open(DST, encoding="utf-8").read() if os.path.exists(DST) else ""
    if body_only(have) == body_only(want):
        print("sqmini.h is in sync with server.c")
        return 0
    if check:
        print("sqmini.h has DRIFTED from ps5-app/onconsole/server.c - run: python tools/ps4_sync_sqmini.py")
        return 1
    io.open(DST, "w", encoding="utf-8", newline="\n").write(want)
    print("sqmini.h updated from server.c")
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Gate: no stray control bytes in tracked source.

WHY THIS EXISTS. A regex in tools/test_payloads.py was written as "\\b(live|have|...)" and ended up
in the file as a literal **backspace byte** - 0x08 - instead of a word boundary. The pattern then
matched nothing, so the check it belonged to passed no matter what the code did. It was only found
because the check was perturbed and stayed green.

That is the second time: an earlier gate in this project was dead for the same reason, two literal
backspaces in a pattern, and the note about it says exactly this - "a gate that could not fail".
Both times the file looked completely normal in an editor and in a terminal, because a terminal
*prints* a backspace by moving the cursor back, so the broken line renders as if it were right.

Nothing in this repo needs a control byte in its source. Tabs, newlines and carriage returns are
ordinary; everything else below 0x20 is a mistake that hides itself.

    python tools/check_control_chars.py
"""
import io
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

ALLOWED = {0x09, 0x0A, 0x0D}          # tab, newline, carriage return
SKIP_EXT = {".elf", ".exe", ".pkg", ".png", ".jpg", ".jpeg", ".gif", ".ico", ".bin", ".db",
            ".shn", ".mc4", ".ttf", ".woff", ".woff2", ".zip", ".pack", ".prx", ".sfo", ".webp"}


def tracked():
    r = subprocess.run(["git", "ls-files"], cwd=ROOT, capture_output=True, text=True)
    if r.returncode != 0:
        return []
    return [f for f in r.stdout.splitlines() if f.strip()]


def main():
    bad = []
    checked = 0
    for rel in tracked():
        if os.path.splitext(rel)[1].lower() in SKIP_EXT:
            continue
        p = os.path.join(ROOT, rel.replace("/", os.sep))
        try:
            data = io.open(p, "rb").read()
        except OSError:
            continue
        if b"\0" in data[:4096]:
            continue                   # binary without a listed extension; not source
        checked += 1
        for lineno, line in enumerate(data.split(b"\n"), 1):
            for b in line:
                v = b if isinstance(b, int) else ord(b)
                if v < 0x20 and v not in ALLOWED:
                    bad.append((rel, lineno, v,
                                line.decode("utf-8", "replace")[:90].replace(chr(v), "<%02X>" % v)))
                    break

    if bad:
        print("check_control_chars: FAIL - %d line(s) carry a control byte" % len(bad))
        for rel, lineno, v, text in bad[:20]:
            print("   %s:%d  0x%02X  %s" % (rel, lineno, v, text))
        print("   A control byte in source is almost always an escape that was eaten on its way in")
        print("   (\\b became 0x08). It renders as if nothing were wrong, and a pattern containing")
        print("   one silently matches nothing.")
        return 1
    print("check_control_chars: OK (%d text file(s), no stray control bytes)" % checked)
    return 0


if __name__ == "__main__":
    sys.exit(main())

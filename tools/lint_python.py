# -*- coding: utf-8 -*-
"""Catch the Python faults that only show up when the line finally runs.

WHY THIS EXISTS. `companion/server.py` is one 9,000-line module and most of it is request handlers
that only execute when someone presses the thing. A name that does not exist is a SyntaxError's
quieter cousin: the file imports, the app starts, every test passes, and the route answers HTTP 500
the first time a user needs it.

That is not hypothetical. A mechanical rewrite added the request body to every `_bridge_for(...)`
call from `_do_POST` downwards - by position in the file - and swept up `_dpi_reload()`, which is a
METHOD, not a branch inside that handler. `body` was simply not a name there. Both "reload the
install engine" controls - the queue dock's tag and Settings > Clear - answered 500 with a
NameError, every press, on a perfectly healthy console. Four separate readers found it; nothing that
ran before shipping did, because nothing presses a recovery button in a test.

WHAT IT CHECKS. pyflakes, which is the smallest tool that answers "is every name defined" without
pretending to be a type checker. Undefined names and duplicate dictionary keys are FATAL; the rest
(unused imports, unused locals) are printed as notes and do not stop a build, because this codebase
keeps some deliberately.

    python tools/lint_python.py            report
    python tools/lint_python.py --check    exit 1 on anything fatal

WITHOUT PYFLAKES INSTALLED it says so and passes, rather than failing a build on a machine that
happens not to have it - the alternative is a gate people learn to skip. It is worth installing:
    python -m pip install pyflakes
"""
import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# Every Python file we ship or run. The tools are here too: one of them writing into a console's
# watch folder is how this project learned that test code is console-facing code.
TARGETS = [os.path.join(ROOT, "companion", "server.py")] + [
    os.path.join(HERE, f) for f in sorted(os.listdir(HERE))
    if f.endswith(".py") and f != "lint_python.py"
]

# A message containing any of these is a guaranteed fault at run time, not a style opinion.
FATAL = ("undefined name", "repeated with different values", "invalid syntax",
         "f-string is missing placeholders", "is assigned to but never used in __all__")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true")
    a = ap.parse_args()

    try:
        import pyflakes  # noqa: F401
    except ImportError:
        print("pyflakes is not installed - skipping. `python -m pip install pyflakes` to enable.")
        return 0

    files = [f for f in TARGETS if os.path.isfile(f)]
    out = subprocess.run([sys.executable, "-m", "pyflakes"] + files,
                         capture_output=True, text=True)
    lines = [l for l in (out.stdout + out.stderr).splitlines() if l.strip()]
    fatal = [l for l in lines if any(k in l for k in FATAL)]
    notes = [l for l in lines if l not in fatal]

    for l in fatal:
        print("  [FATAL] " + l.replace(ROOT + os.sep, ""))
    for l in notes:
        print("  [ note] " + l.replace(ROOT + os.sep, ""))

    print("\n%d file(s) checked - %d fatal, %d note(s)" % (len(files), len(fatal), len(notes)))
    if a.check and fatal:
        print("A name that does not exist is a 500 on the route that uses it. Fix these.")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())

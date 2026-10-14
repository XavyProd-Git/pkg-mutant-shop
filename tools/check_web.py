#!/usr/bin/env python3
"""Refuse to build if web/index.html contains JavaScript that will not parse.

The whole UI is one script block shared by both artifacts, so a single bad token —
a real newline inside a "..." literal, say — blanks the app on the PS5 *and* the PC
at once, and the servers keep answering happily so nothing looks wrong from outside.
That is expensive to notice and trivial to catch here.

Uses node when it is on the path (a real parse). Falls back to a scanner that finds
the class of damage a generated edit actually causes: an unterminated string literal.
Exits non-zero on failure so the build stops.
"""
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
INDEX = os.path.join(os.path.dirname(HERE), "web", "index.html")


def script_blocks(html):
    """Inline <script> bodies only — a src= tag has nothing to check."""
    out = []
    for m in re.finditer(r"<script([^>]*)>(.*?)</script>", html, re.S | re.I):
        if "src=" in m.group(1).lower():
            continue
        start_line = html.count("\n", 0, m.start(2)) + 1
        out.append((start_line, m.group(2)))
    return out


def scan_strings(src, base_line):
    """Report string literals that run off the end of their line.

    Walks the source as a small state machine so quotes inside comments, inside
    template literals, and inside regex literals do not raise false alarms.
    """
    problems = []
    i, n = 0, len(src)
    line = base_line
    state = "code"
    quote = ""
    str_line = 0
    prev_sig = ""          # last significant char, decides regex vs division
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if c == "\n":
            line += 1
        if state == "code":
            if c == "/" and nxt == "/":
                state = "line_comment"; i += 2; continue
            if c == "/" and nxt == "*":
                state = "block_comment"; i += 2; continue
            if c == "/" and prev_sig in "(,=:[!&|?{};+-*%~^<>" :
                state = "regex"; i += 1; continue
            if c in "'\"":
                state = "string"; quote = c; str_line = line; i += 1; continue
            if c == "`":
                state = "template"; i += 1; continue
            if not c.isspace():
                prev_sig = c
            i += 1
            continue
        if state == "line_comment":
            if c == "\n":
                state = "code"
            i += 1
            continue
        if state == "block_comment":
            if c == "*" and nxt == "/":
                state = "code"; i += 2; continue
            i += 1
            continue
        if state == "regex":
            if c == "\\":
                i += 2; continue
            if c == "\n":                      # a regex cannot span lines
                state = "code"
            elif c == "/":
                state = "code"
            i += 1
            continue
        if state == "template":
            if c == "\\":
                i += 2; continue
            if c == "`":
                state = "code"
            i += 1
            continue
        if state == "string":
            if c == "\\":
                i += 2; continue               # covers \" \' and \<newline>
            if c == "\n":
                problems.append((str_line, quote))
                state = "code"
            elif c == quote:
                state = "code"
            i += 1
            continue
    return problems


# Globals the page may legitimately call without defining. Kept short on purpose: every name
# added here is a name the gate stops protecting.
KNOWN_GLOBALS = set("""
if for while switch catch return typeof instanceof new delete void function
alert confirm prompt fetch setTimeout setInterval clearTimeout clearInterval requestAnimationFrame
parseInt parseFloat isNaN isFinite encodeURIComponent decodeURIComponent encodeURI decodeURI
String Number Boolean Array Object Date Math JSON RegExp Error Promise Map Set WeakMap Symbol
document window navigator location history localStorage sessionStorage console screen
XMLHttpRequest FormData Blob URL Image Audio Event CustomEvent MutationObserver IntersectionObserver
escape unescape btoa atob getComputedStyle matchMedia
cancelAnimationFrame DOMParser TextDecoder TextEncoder Notification
WebSocket Worker Uint8Array Int32Array Float64Array ArrayBuffer DataView
""".split())
# structuredClone, queueMicrotask, AbortController and Intl are NOT in the list: the PS5 browser
# does not have them, and listing them here meant a call to one passed the gate and threw on the
# console. Nothing in the page uses them today; a future use should fail here, not there.


def check_calls(src, base_line):
    """Report calls to functions that are defined nowhere in this script.

    Deliberately narrow: only a bare `name(` at a call site, only when `name` is not declared
    anywhere as a function, var, parameter-ish assignment or property, and not a known global.
    A false alarm here would be worse than a miss, because the build stops on it."""
    problems = []
    # Strip comments and string/template literals so their contents cannot look like code.
    code = re.sub(r"/\*.*?\*/", " ", src, flags=re.S)
    code = re.sub(r"(^|[^:])//[^\n]*", lambda m: m.group(1), code)
    code = re.sub(r'"(?:[^"\\\n]|\\.)*"', '""', code)
    code = re.sub(r"'(?:[^'\\\n]|\\.)*'", "''", code)
    code = re.sub(r"`(?:[^`\\]|\\.)*`", "``", code, flags=re.S)

    defined = set()
    defined |= set(re.findall(r"\bfunction\s+([A-Za-z_$][\w$]*)", code))
    defined |= set(re.findall(r"\bvar\s+([A-Za-z_$][\w$]*)", code))
    # `var a=1, b=function(){}` and `X.y = function` style
    defined |= set(re.findall(r"[,{]\s*([A-Za-z_$][\w$]*)\s*=\s*function", code))
    for params in re.findall(r"\bfunction\b[^(]*\(([^)]*)\)", code):
        for p in params.split(","):
            p = p.strip()
            if re.match(r"^[A-Za-z_$][\w$]*$", p):
                defined.add(p)

    seen = set()
    for m in re.finditer(r"(^|[^.\w$])([A-Za-z_$][\w$]*)\s*\(", code):
        name = m.group(2)
        if name in defined or name in KNOWN_GLOBALS or name in seen:
            continue
        seen.add(name)
        line = base_line + code.count("\n", 0, m.start(2))
        problems.append((line, name))
    return problems


def code_only(src):
    """`src` with every comment, string, template and regex literal blanked to spaces (newlines
    kept, so line numbers survive). The same state machine as scan_strings, because the regex
    stripping in check_calls loses its place on a string that contains an escaped quote followed
    by an apostrophe - and a rule that fires on text inside a string stops a build for nothing."""
    out = []
    i, n = 0, len(src)
    state, quote, prev_sig = "code", "", ""
    templates = []
    while i < n:
        c = src[i]
        nxt = src[i + 1] if i + 1 < n else ""
        if state == "code":
            if c == "/" and nxt == "/":
                state = "line_comment"; out.append("  "); i += 2; continue
            if c == "/" and nxt == "*":
                state = "block_comment"; out.append("  "); i += 2; continue
            if c == "/" and prev_sig in "(,=:[!&|?{};+-*%~^<>":
                state = "regex"; out.append(" "); i += 1; continue
            if c in "'\"":
                state = "string"; quote = c; out.append(" "); i += 1; continue
            if c == "`":
                state = "template"; templates.append(i); out.append(" "); i += 1; continue
            if not c.isspace():
                prev_sig = c
            out.append(c); i += 1; continue
        if state == "line_comment":
            if c == "\n":
                state = "code"
            out.append(c if c == "\n" else " "); i += 1; continue
        if state == "block_comment":
            if c == "*" and nxt == "/":
                state = "code"; out.append("  "); i += 2; continue
            out.append(c if c == "\n" else " "); i += 1; continue
        if state == "regex":
            if c == "\\":
                out.append("  "); i += 2; continue
            if c in "\n/":
                state = "code"
            out.append(c if c == "\n" else " "); i += 1; continue
        if state == "template":
            if c == "\\":
                out.append("  "); i += 2; continue
            if c == "`":
                state = "code"
            out.append(c if c == "\n" else " "); i += 1; continue
        if state == "string":
            if c == "\\":
                out.append("  "); i += 2; continue
            if c == "\n" or c == quote:
                state = "code"
            out.append(c if c == "\n" else " "); i += 1; continue
    return "".join(out), templates


# The page runs in the PS5's WebKit, which is ES5 plus a few named extras (Promise, fetch, Set).
# node --check accepts ES2020, so an arrow function or a template literal parsed clean here and
# blanked the app on the console. These are the post-ES5 forms a generated edit actually
# produces; each is matched only in code, never in a string or a comment.
ES5_RULES = (
    (r"=>", "an arrow function (=>) - write function(){}"),
    (r"\b(let|const)\s+[A-Za-z_$]", "let/const - use var"),
    (r"\basync\s+function\b", "async function - use Promise.then()"),
    (r"\bawait\b", "await - use Promise.then()"),
    (r"\bclass\s+[A-Za-z_$][\w$]*\s*(\{|extends\b)", "a class declaration - use function + prototype"),
    (r"\bfunction\s*\*|\byield\b", "a generator - not available"),
)


def check_ps5_isms(src, base_line):
    """Things the PS5's browser does not do, whatever the parser thinks."""
    out = []
    code, templates = code_only(src)
    for m in re.finditer(r"classList\.toggle\s*\([^),]*,", code):
        out.append((base_line + code.count("\n", 0, m.start()),
                    "classList.toggle(name, force) - the second argument is not supported; "
                    "use add()/remove()"))
    for pos in templates:
        out.append((base_line + src.count("\n", 0, pos),
                    "a template literal (`...`) - the PS5 browser does not parse it; use \"\" + concatenation"))
    for pat, why in ES5_RULES:
        for m in re.finditer(pat, code):
            out.append((base_line + code.count("\n", 0, m.start()), why))
    return out


def check_with_node(src):
    """A real parse. Returns (ok, message) or (None, '') when node is unavailable."""
    node = shutil.which("node")
    if not node:
        return None, ""
    fd, path = tempfile.mkstemp(suffix=".js")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(src)
        p = subprocess.run([node, "--check", path], capture_output=True, text=True)
        if p.returncode == 0:
            return True, ""
        msg = (p.stderr or p.stdout).strip().splitlines()
        return False, "\n".join(msg[:8])
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else INDEX
    if not os.path.exists(path):
        print("check_web: %s not found" % path, file=sys.stderr)
        return 1
    with open(path, encoding="utf-8", errors="replace") as fh:
        html = fh.read()

    blocks = script_blocks(html)
    if not blocks:
        print("check_web: no inline script found in %s" % os.path.basename(path), file=sys.stderr)
        return 1

    failed = False
    for base_line, src in blocks:
        for bad_line, q in scan_strings(src, base_line):
            failed = True
            print("check_web: FAILED %s:%d — %s string literal is not closed on its line "
                  "(a real newline where \\n was meant?)"
                  % (os.path.basename(path), bad_line,
                     "single-quoted" if q == "'" else "double-quoted"),
                  file=sys.stderr)

    for base_line, src in blocks:
        for line, name in check_calls(src, base_line):
            failed = True
            print("check_web: FAILED %s:%d - calls %s() which is defined nowhere. This parses "
                  "cleanly and throws at runtime." % (os.path.basename(INDEX), line, name),
                  file=sys.stderr)
        for line, why in check_ps5_isms(src, base_line):
            failed = True
            print("check_web: FAILED %s:%d - %s" % (os.path.basename(INDEX), line, why),
                  file=sys.stderr)

    used_node = False
    for _, src in blocks:
        ok, msg = check_with_node(src)
        if ok is None:
            continue
        used_node = True
        if not ok:
            failed = True
            print("check_web: FAILED — node could not parse the UI script:\n%s" % msg,
                  file=sys.stderr)

    if failed:
        print("check_web: build stopped. Both the ELF and the EXE embed this page, "
              "so shipping it would blank the app on every device.", file=sys.stderr)
        return 1

    print("check_web: UI script OK (%d block%s%s)"
          % (len(blocks), "" if len(blocks) == 1 else "s",
             ", node-parsed" if used_node else ", scanned"))
    return 0


if __name__ == "__main__":
    sys.exit(main())

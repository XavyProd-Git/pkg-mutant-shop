#!/usr/bin/env python3
"""Every word PKG MUTANT SHOP says, pulled out of the source that says it.

WHY THIS IS GENERATED AND NOT WRITTEN
-------------------------------------
The first version of this list was written by hand, and it was out of date within a day - a toast
was reworded in server.c and the table still quoted the old sentence. A hand-kept list of strings
is a second copy of the strings, and second copies drift. This reads the real ones.

  python tools/message_report.py            -> research/message-reference.html
  python tools/message_report.py --check    -> exit 1 if any message breaks the house style

HOUSE STYLE (enforced by --check)
  * console toasts: at most two lines; line 1 is the event, line 2 is what it means or what to do
  * the words "PKG MUTANT SHOP" appear only when the shop itself is the subject
  * no raw error codes, JSON, or file paths on a television
  * no bare-verb failures ("Failed", "Rescan failed") anywhere
  * no reference to software we do not ship (etaHEN, Elf Arsenal, DPI, :12800) - anywhere

WHAT IS READ: every notify()/notifyf() in the two console binaries, every literal in the first
argument of every toast() in index.html (t("key") resolved to its English sentence), the ERR_TEXT
table, every queue-row msg= and every "error"/"message"/"detail" sentence in companion/server.py.
"""
import argparse
import html
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
OUT = os.path.join(ROOT, "research", "message-reference.html")

CONSOLE_C = os.path.join(ROOT, "ps5-app", "onconsole", "server.c")
PROBE_C = os.path.join(ROOT, "ps5-app", "onconsole", "installer_probe.c")
# The PS4 half of the shop. Both payloads say things to a person on a television, so both are held
# to the same rules - a gate that covered only one binary would have let the PS4 build drift.
PS4_C = os.path.join(ROOT, "ps4-app", "onconsole", "server_ps4.c")
# The PS4's dashboard app. Small, but everything it says lands on a television - and it had already
# drifted, naming the jailbreak software in a message, which this file has banned since the rule was
# written. A gate that does not read a binary cannot hold it to anything.
PS4_TILE_C = os.path.join(ROOT, "ps4-app", "tile-pkg", "pms", "main.c")
COMPANION = os.path.join(ROOT, "companion", "server.py")
WEB = os.path.join(ROOT, "web", "index.html")

# Words that should not appear in anything a user reads. The docstring promised etaHEN, Elf
# Arsenal, DPI and :12800; the tuple only carried "dpiv2", so "DPI v2 host on :12800" sailed
# through the gate that was written for it. Regexes, so that "dpi" matches the word and not the
# inside of another one. Nothing shipped is exempt today: an ALLOW_WHERE used to name two
# "remove that software" controls that no longer exist anywhere in the three sources (and web
# rows all carry where="app toast", so it could never have matched them anyway). If a control
# that removes third-party software returns, exempt it here by its function name - do not
# pretend the rule does not exist.
BANNED = (r"etahen", r"elf\s*arsenal", r"\bdpi\b", r"dpiv2", r":12800\b", r"goldhen")
BARE_VERBS = ("failed", "error", "failed.", "rescan failed", "scan failed", "hash failed",
              "copy failed", "move failed", "verify failed", "toggle failed", "patch failed",
              "retry failed", "reload failed", "could not save")


def _first_arg(call):
    """The text of a call's FIRST argument only, given `name(...)`.

    Taking every literal in the call swept up the arguments too, so a plural suffix written as
    `n == 1 ? "" : "s"` was concatenated onto the end of the sentence and the report quoted
    "...written to the running game - s". Stop at the first comma that is not inside brackets,
    a string, or a ternary's colon.
    """
    i = call.find("(")
    if i < 0:
        return ""
    depth, j, in_str, esc = 0, i, False, False
    while j < len(call):
        c = call[j]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
        elif c == '"':
            in_str = True
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
            if depth == 0:
                return call[i + 1:j]
        elif c == "," and depth == 1:
            return call[i + 1:j]
        j += 1
    return call[i + 1:]


def _join_c_string(text):
    """Turn a run of C string literals ("a" "b") into one string, keeping \\n as a marker."""
    parts = re.findall(r'"((?:[^"\\]|\\.)*)"', _first_arg(text) if "(" in text else text)
    return "".join(parts)


def read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def console_messages():
    """Every notify()/notifyf()/notify_sync() call in the console binaries."""
    out = []
    for path, who in ((CONSOLE_C, "shop ELF"), (PROBE_C, "installer ELF"),
                      (PS4_C, "PS4 shop ELF"), (PS4_TILE_C, "PS4 app")):
        if not os.path.exists(path):
            continue
        src = read(path)
        lines = src.split("\n")
        for m in re.finditer(r'\bnotify(?:f|_sync|_icon)?\s*\(', src):
            start = m.start()
            # take up to the matching close paren, so multi-line literals come along
            depth, i = 0, m.end() - 1
            while i < len(src):
                if src[i] == "(":
                    depth += 1
                elif src[i] == ")":
                    depth -= 1
                    if depth == 0:
                        break
                i += 1
            call = src[start:i + 1]
            body = _join_c_string(call)
            if not body or body.strip() in ("%s", ""):
                continue
            lineno = src.count("\n", 0, start) + 1
            # The enclosing function, for "where". Signatures are matched at COLUMN 0 only, and a
            # multi-line signature is matched on its first line - an earlier version required the
            # opening brace on the same line, so every message inside a function whose arguments
            # wrapped was attributed to whichever function was defined above it.
            where = ""
            for j in range(lineno - 1, -1, -1):
                mm = re.match(r'^(?:static\s+)?[A-Za-z_][\w]*[\w \*]*?\b(\w+)\s*\((?!\s*\))',
                              lines[j])
                if mm and not lines[j].rstrip().endswith(";"):
                    where = mm.group(1)
                    break
            out.append({"text": body, "file": os.path.basename(path), "line": lineno,
                        "where": where or who, "who": who})
    return out


def _en_dict(src):
    """{key: English text} out of the I18N literal, so a toast written as toast(t("key")) is
    linted by the sentence the user reads, not by the key."""
    i = src.find("var I18N=")
    j = src.find("\n  en:{", i) if i >= 0 else -1
    if j < 0:
        return {}
    # THE END OF THE ENGLISH BLOCK IS THE NEXT LANGUAGE, and find("\n  ") did not locate it: two
    # spaces are a PREFIX of the four that every key line is indented by, so the block ended at its
    # own second line and this dictionary held only the eleven keys sharing the opening line. Every
    # toast written as toast(t("key")) therefore resolved to "" and was skipped by the house-style
    # gate entirely - silent, and exactly the gutting that routing toasts through t() was once
    # postponed to avoid.
    nxt = re.search(r"\n  [a-z]{2}:\{", src[j + 6:])
    body = src[j:j + 6 + nxt.start()] if nxt else src[j:]
    return dict((m.group(1), m.group(2))
                for m in re.finditer(r'([A-Za-z_][A-Za-z0-9_]*):"((?:[^"\\]|\\.)*)"', body))


def _js_literals(expr):
    """Every double-quoted literal in a JS expression, in order."""
    return re.findall(r'"((?:[^"\\]|\\.)*)"', expr)


def web_messages():
    """Every sentence the app can put in a toast.

    ONLY toast("literal") USED TO BE SEEN. 23 of the 123 toast() calls build their text from an
    expression - toast(r.message || "Already installed"), toast(on ? "Enabled" : "Disabled"),
    toast(t("key")) - and every literal in those was outside the gate, so a bare-verb or a banned
    name there shipped unchecked. Now every literal inside the FIRST argument counts, a t("key")
    or tsub("key", ...) inside it is replaced by the English sentence behind the key, and the
    ERR_TEXT table - the sentences errText() substitutes for the server's codes - is linted too."""
    src = read(WEB)
    en = _en_dict(src)
    out = []
    key_call = r'(?<![A-Za-z0-9_.$])(?:t|tsub)\(\s*"([A-Za-z0-9_]+)"'
    # confirm() and prompt() TOO, not just toast(). They are the longest sentences this app ever
    # shows - a clean reinstall, deleting a backup, stopping every payload before rest mode - and
    # they sat outside this gate entirely, which is the opposite of where the care should go.
    for m in re.finditer(r'(?<![A-Za-z0-9_.$])(?:toast|toastHtml|confirm|prompt)\(', src):
        arg = _first_arg(src[m.start():m.start() + 4000])
        lineno = src.count("\n", 0, m.start()) + 1
        # keys first, so a key's literal is not also reported as a sentence of its own
        texts = [en.get(km.group(1), "") for km in re.finditer(key_call, arg)]
        texts += _js_literals(re.sub(key_call, "", arg))
        for t in texts:
            if not t.strip():
                continue
            out.append({"text": t, "file": "web/index.html", "line": lineno,
                        "where": "app toast", "who": "app"})
    # the ones built from an expression get listed by their fallback sentence instead
    for m in re.finditer(r'errText\([^,]+,\s*("(?:[^"\\]|\\.)*")\s*\)', src):
        lineno = src.count("\n", 0, m.start()) + 1
        out.append({"text": m.group(1)[1:-1], "file": "web/index.html", "line": lineno,
                    "where": "app toast (fallback)", "who": "app"})
    # ERR_TEXT: what the app says in place of a server code
    i = src.find("var ERR_TEXT={")
    j = src.find("\n};", i) if i >= 0 else -1
    if i >= 0 and j > i:
        for m in re.finditer(r'"(?:[^"\\]|\\.)*"\s*:\s*"((?:[^"\\]|\\.)*)"', src[i:j]):
            lineno = src.count("\n", 0, i + m.start()) + 1
            out.append({"text": m.group(1), "file": "web/index.html", "line": lineno,
                        "where": "ERR_TEXT", "who": "app"})
    return out


# KEYS RENDERED AS FRAGMENTS, NOT AS SENTENCES. These are composed into a queue status label -
# the page builds err + " " + t("q_failed"), and the state word beside it - so "failed" and "Error"
# are correct English in the only place they appear. Holding them to the bare-verb rule would be a
# false positive, and a gate that cries wolf gets switched off.
DICT_FRAGMENT_KEYS = ("q_failed", "msg_q_state_error")


def dict_messages():
    """Every sentence in the I18N dictionary, in EVERY language.

    web_messages() only sees what reaches a toast()/confirm()/prompt() call, so a sentence that is
    defined and translated but NOT YET WIRED to a control sat outside this gate completely. That is
    exactly how a discovery message naming software this project does not ship lived in seven
    languages under a green check: nothing called it, so nothing looked at it, and the day someone
    wired it up it would have shipped without ever being read. A string that SHIPS is checked
    whether or not anything calls it yet.

    Checked, not listed: the report's job is to show the English the shop says, and 663 keys across
    15 languages would drown it.
    """
    src = read(WEB)
    i = src.find("var I18N=")
    if i < 0:
        return []
    kv = re.compile(r'([A-Za-z_][A-Za-z0-9_]*):"((?:[^"\\]|\\.)*)"')
    out = []
    for m in re.finditer(r"\n  ([a-z]{2}):\{", src[i:]):
        code = m.group(1)
        j = src.index("{", i + m.start())
        depth, k = 0, j
        while k < len(src):
            if src[k] == "{":
                depth += 1
            elif src[k] == "}":
                depth -= 1
                if depth == 0:
                    break
            k += 1
        for km in kv.finditer(src[j:k + 1]):
            if km.group(1) in DICT_FRAGMENT_KEYS or not km.group(2).strip():
                continue
            out.append({"text": km.group(2), "file": "web/index.html",
                        "line": src.count("\n", 0, j + km.start()) + 1,
                        "where": "dictionary [%s] %s" % (code, km.group(1)), "who": "app"})
    return out


def queue_messages():
    src = read(COMPANION)
    out = []
    for m in re.finditer(r'msg=("(?:[^"\\]|\\.)*"(?:\s*\n?\s*"(?:[^"\\]|\\.)*")*)', src):
        text = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(1)))
        if not text or text.startswith("⟳"):
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        out.append({"text": text, "file": "companion/server.py", "line": lineno,
                    "where": "queue row", "who": "queue"})
    return out


def server_messages():
    """The companion's own sentences: every "error"/"message"/"detail": "..." literal in
    server.py. errText() in the UI shows any of these to the user unchanged when it is shaped
    like a sentence, so they are user-facing whether or not a toast() names them. Python's
    implicit concatenation across lines is joined the way the interpreter joins it. Codes such
    as "no_space" are keys the UI translates, not sentences; they are skipped."""
    src = read(COMPANION)
    out = []
    pat = re.compile(r'"(error|message|detail)":\s*\(?\s*("(?:[^"\\]|\\.)*"'
                     r'(?:\s*\n?\s*"(?:[^"\\]|\\.)*")*)')
    for m in pat.finditer(src):
        text = "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', m.group(2)))
        if not text.strip() or re.match(r"^[a-z0-9_]+$", text):
            continue
        lineno = src.count("\n", 0, m.start()) + 1
        out.append({"text": text, "file": "companion/server.py", "line": lineno,
                    "where": "API %s" % m.group(1), "who": "companion"})
    return out


def lint(rows):
    """Return a list of (row, complaint). Empty means the whole app is on style."""
    bad = []
    for r in rows:
        t = r["text"]
        low = t.lower()
        if any(re.search(b, low) for b in BANNED):
            bad.append((r, "names software we do not ship"))
        if low.strip().rstrip(".") in BARE_VERBS:
            bad.append((r, "bare-verb failure - says nothing about what to do"))
        if r["who"] not in ("app", "companion") and t.count("\\n") > 1:
            bad.append((r, "more than two lines on a television"))
        # No exception for messages that also carry a %s. That loophole let "%s could not be
        # installed (0x%08X)" through: a sentence in front of a hex code is still a hex code on
        # a television. The code belongs in the install log, which every one of these writes.
        if re.search(r'0x%0?8[xX]|0x[0-9A-Fa-f]{8}\b|\{\\"', t):
            bad.append((r, "puts a raw code or JSON on screen"))
        if r["who"] in ("shop ELF", "installer ELF", "PS4 shop ELF", "PS4 app") and t.startswith("PKG MUTANT SHOP\\n"):
            bad.append((r, "app name used as a title line - the icon already says who is speaking"))
    return bad


PAGE = """<title>What The Shop Says</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link rel="stylesheet" href="https://fonts.googleapis.com/css2?family=IBM+Plex+Mono:wght@400;600&family=Newsreader:opsz,wght@6..72,400;6..72,600&display=swap">
<style>
  :root{
    --ink:#12100f; --ink-soft:#514a45; --ink-faint:#8b817a;
    --paper:#fbf9f6; --card:#ffffff; --rule:#e6e0d8;
    --mark:#b4442e; --mark-soft:#f3e3de; --good:#3f7d55; --warn:#9a6b12;
    --mono:"IBM Plex Mono",ui-monospace,"SF Mono",Menlo,Consolas,monospace;
  }
  @media (prefers-color-scheme: dark){
    :root:not([data-theme="light"]){
      --ink:#f0ebe5; --ink-soft:#b6ada5; --ink-faint:#7d746d;
      --paper:#14120f; --card:#1c1916; --rule:#2f2a25;
      --mark:#e0765c; --mark-soft:#33221d; --good:#7fb98f; --warn:#d6a54a;
    }
  }
  :root[data-theme="dark"]{
    --ink:#f0ebe5; --ink-soft:#b6ada5; --ink-faint:#7d746d;
    --paper:#14120f; --card:#1c1916; --rule:#2f2a25;
    --mark:#e0765c; --mark-soft:#33221d; --good:#7fb98f; --warn:#d6a54a;
  }
  body{background:var(--paper);color:var(--ink);margin:0;
       font:16px/1.62 Newsreader,"Iowan Old Style","Palatino Linotype",Palatino,Georgia,serif;}
  .wrap{max-width:1180px;margin:0 auto;padding:48px 22px 90px}
  h1{font-size:38px;line-height:1.1;margin:0 0 6px;letter-spacing:-.02em;text-wrap:balance}
  .sub{color:var(--ink-soft);margin:0 0 34px;font-size:16px}
  h2{font-size:12px;letter-spacing:.16em;text-transform:uppercase;color:var(--mark);
     margin:44px 0 12px;font-family:var(--mono);font-weight:600}
  .count{color:var(--ink-faint);font-weight:400;letter-spacing:.06em}
  .tablewrap{overflow-x:auto;border:1px solid var(--rule);border-radius:10px;background:var(--card)}
  table{border-collapse:collapse;width:100%;min-width:760px}
  th{font-family:var(--mono);font-size:10px;letter-spacing:.13em;text-transform:uppercase;
     color:var(--ink-faint);text-align:left;padding:11px 14px;border-bottom:1px solid var(--rule);
     font-weight:600;background:var(--card);position:sticky;top:0}
  td{padding:11px 14px;border-bottom:1px solid var(--rule);vertical-align:top}
  tr:last-child td{border-bottom:0}
  .msg{font-family:var(--mono);font-size:12.5px;line-height:1.65;white-space:pre-wrap;
       color:var(--ink);max-width:640px}
  .msg .l2{color:var(--ink-soft)}
  .msg var{color:var(--mark);font-style:normal;background:var(--mark-soft);
           padding:0 3px;border-radius:3px}
  .where{font-family:var(--mono);font-size:11px;color:var(--ink-faint);white-space:nowrap}
  .ok{color:var(--good);font-family:var(--mono);font-size:11px}
  .note{background:var(--mark-soft);border-left:3px solid var(--mark);padding:14px 18px;
        border-radius:0 8px 8px 0;margin:0 0 30px;color:var(--ink-soft);font-size:14px}
  .note b{color:var(--ink)}
  footer{margin-top:60px;padding-top:18px;border-top:1px solid var(--rule);
         color:var(--ink-faint);font-size:12.5px;font-family:var(--mono)}
</style>
<div class="wrap">
<h1>What the shop says</h1>
<p class="sub">Every message PKG MUTANT SHOP puts in front of you &mdash; on the television and in the
app &mdash; read straight out of the code that says it.</p>
<div class="note">
  <b>How to read the messages.</b> <var>%s</var> is filled in when the message is shown: a game name,
  a count, a drive. A second line is shown underneath in grey &mdash; on the PS5 that is the smaller
  line under the title. Every console message carries the shop icon, which is why almost none of them
  say &ldquo;PKG MUTANT SHOP&rdquo;: the artwork already says who is speaking.
</div>
__BODY__
<footer>__FOOT__</footer>
</div>
"""


def fmt(text):
    parts = text.split("\\n")
    esc = [html.escape(p) for p in parts]
    esc = [re.sub(r'(%\w|%\d*\.?\d*[dsfxX]|%08X|%lld)', r'<var>\1</var>', p) for p in esc]
    if len(esc) == 1:
        return esc[0]
    return esc[0] + "\n" + '<span class="l2">' + "\n".join(esc[1:]) + "</span>"


def table(rows, cols=("Message", "Where it comes from")):
    out = ['<div class="tablewrap"><table><thead><tr>']
    out.append("<th>%s</th><th>%s</th>" % cols)
    out.append("</tr></thead><tbody>")
    for r in sorted(rows, key=lambda x: x["text"].lower()):
        out.append('<tr><td class="msg">%s</td><td class="where">%s<br>%s:%d</td></tr>'
                   % (fmt(r["text"]), html.escape(r["where"]), html.escape(r["file"]), r["line"]))
    out.append("</tbody></table></div>")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if any message breaks the house style")
    args = ap.parse_args()

    con = console_messages()
    web = web_messages()
    que = queue_messages() + server_messages()
    allrows = con + web + que
    # The dictionary is CHECKED but not rendered into the report - see dict_messages. With --check
    # off this is exactly allrows, so the generated page is unchanged byte for byte.
    check_rows = allrows + (dict_messages() if args.check else [])

    bad = lint(check_rows)
    if args.check:
        for r, why in bad:
            print("%s:%d  %s\n    %r" % (r["file"], r["line"], why, r["text"][:110]))
        print("%d message(s) checked, %d off style" % (len(check_rows), len(bad)))
        return 1 if bad else 0

    body = []
    body.append('<h2>On the television <span class="count">&mdash; %d</span></h2>' % len(con))
    body.append(table(con))
    body.append('<h2>In the app <span class="count">&mdash; %d</span></h2>' % len(web))
    body.append(table(web))
    body.append('<h2>In the install queue <span class="count">&mdash; %d</span></h2>' % len(que))
    body.append(table(que))

    foot = "%d messages &middot; generated by tools/message_report.py &middot; %s" % (
        len(allrows), "off style: %d" % len(bad) if bad else "all on style")
    page = PAGE.replace("__BODY__", "\n".join(body)).replace("__FOOT__", foot)
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="") as f:
        f.write(page)
    print("wrote %s (%d messages, %d off style)" % (OUT, len(allrows), len(bad)))
    return 0


if __name__ == "__main__":
    sys.exit(main())

# -*- coding: utf-8 -*-
"""Translation coverage for web/index.html.   Run: python tools/i18n_report.py [--check]

WHY THIS EXISTS. The language selector shipped for a long time translating only a fraction of the
app, and nothing noticed: the dictionary was healthy, the wiring was not. A string only reaches a
translation if somebody remembers to route it through t() or a data-i18n attribute, and forgetting
is silent - the app just quietly stays English for that one label, on every device, in every
language. This turns that into a number you can look at, and with --check into a build gate.

WHAT IT CHECKS
  1. Every language has every key the app actually uses.  A missing key is not fatal at runtime -
     t() falls back to English - but it is a hole in the translation and it should be visible.
  2. Every key the app uses is DEFINED.  A typo'd key silently renders as the key itself, which
     looks like "gp_installed_to" on screen.
  3. Keys defined but never used, so the dictionary does not rot with dead weight.
  4. A rough count of user-visible text still hardcoded in the markup, as a direction of travel.

WHAT IT DELIBERATELY DOES NOT DO
  It does not try to prove a string SHOULD be translated. Game names, file names, paths, title ids
  and API values must stay exactly as they are, and no heuristic can reliably tell those from a
  label. Item 4 is a hint for a human, never a failure.
"""
import io
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(ROOT, "web", "index.html")


def read():
    return io.open(HTML, encoding="utf-8").read()


def dict_block(s):
    """The I18N literal, from 'var I18N={' to the line that closes it."""
    i = s.find("var I18N=")
    if i < 0:
        return ""
    depth, j = 0, s.index("{", i)
    start = j
    while j < len(s):
        if s[j] == "{":
            depth += 1
        elif s[j] == "}":
            depth -= 1
            if depth == 0:
                return s[start:j + 1]
        j += 1
    return ""


def languages(block):
    """{code: set(keys)} for every language in the dictionary."""
    out = {}
    # each language starts at  <two spaces><code>:{  at the top level of the literal
    for m in re.finditer(r"\n\s{2}([a-z]{2}):\{", block):
        code = m.group(1)
        depth, j = 0, block.index("{", m.start())
        start = j
        while j < len(block):
            if block[j] == "{":
                depth += 1
            elif block[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = block[start:j + 1]
        out[code] = set(re.findall(r"[\{,]\s*([A-Za-z_][A-Za-z0-9_]*)\s*:", body))
    return out


def _first_arg(s, start):
    """The text of the first argument of the call whose '(' is at s[start]. Stops at the first
    comma that is not inside brackets or a string, or at the matching close paren."""
    depth, j, q, esc = 0, start, None, False
    while j < len(s):
        c = s[j]
        if q:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == q:
                q = None
        elif c in "\"'":
            q = c
        elif c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
            if depth == 0:
                return s[start + 1:j]
        elif c == "," and depth == 1:
            return s[start + 1:j]
        j += 1
    return s[start + 1:]


def used_keys(s):
    """Every key the app actually asks for.

    THIS USED TO SEE ONLY t("literal"). The app also reaches keys through tsub("key", {...}),
    through t("key", extra) and through ternaries - t(n === 1 ? "one" : "many"),
    tsub(on ? "gp_mod_enabled" : "gp_mod_disabled", {...}) - and none of those were counted. So
    29 live keys were reported as "never used (dead weight)", inviting their deletion, and the
    eight languages added in 3.60.0 shipped without any of them while --check printed 100%. The
    fallback to English hid it on screen. Now every string literal inside the FIRST argument of a
    t( or tsub( call counts as used, whatever expression it sits in."""
    keys = set()
    for attr in ("data-i18n", "data-i18n-ph", "data-i18n-tip", "data-i18n-aria"):
        keys |= set(re.findall(attr + r'="([^"]+)"', s))
    for m in re.finditer(r'(?<![A-Za-z0-9_.$])(?:t|tsub)\(', s):
        arg = _first_arg(s, m.end() - 1)
        # A literal is a KEY only where the value of the expression can be it: the whole
        # argument, or an arm of a ternary. t(kind === "update" ? "gp_kind_patch" : "gp_kind_dlc")
        # compares against "update" - that is not a key, and counting it reported a phantom
        # "used but not defined" failure.
        for lm in re.finditer(r'"([A-Za-z0-9_]+)"', arg):
            before = arg[:lm.start()].rstrip()
            after = arg[lm.end():].lstrip()
            if before and before[-1] not in "?:":
                continue
            if after and after[0] not in "?:":
                continue
            keys.add(lm.group(1))

    # A KEY REACHED THROUGH A VARIABLE IS STILL A KEY.
    #
    # errText() looks a code up in a table of KEY NAMES and calls t() on the result -
    # t(ERR_KEY[raw]) - so not one of those nineteen sentences appears as a literal inside a t(
    # call. They read here as "never used", which invites deleting them and, worse, excuses every
    # language from carrying them: the --check that gates a build would have printed 100% while a
    # PS4 owner in Spanish got every failure message in English.
    #
    # So a key-shaped literal that names a DEFINED key counts as used wherever it appears in the
    # script. It cannot invent a key - it only matches names the English dictionary already has -
    # and the cost of being wrong in this direction is a language carrying one sentence it does
    # not need, against a build gate that silently passes.
    # AN UNDERSCORE IS WHAT MAKES IT KEY-SHAPED. Matching any literal that happens to equal a key
    # name pulled in "rescan" - an ordinary word that appears in the page as a plain string and
    # also, by coincidence, names a key. Every key this family cares about is prefixed
    # (msg_*, tm_*, gp_*, ui_*), so requiring the underscore keeps the real ones and drops words.
    defined = set(languages(dict_block(s)).get("en", {}))
    for m in re.finditer(r'"([A-Za-z][A-Za-z0-9]*_[A-Za-z0-9_]{2,})"', s):
        if m.group(1) in defined:
            keys.add(m.group(1))
    return keys


# THE CEILING, AND IT ONLY EVER COMES DOWN.
#
# Coverage was measured against the keys the app USES, so 129 sentences that were never keys at all
# were invisible to it: switch the app to any of the fourteen other languages and almost everything
# that HAPPENED still answered in English - moving a game, verifying a file, deleting, retrying,
# the rest-mode confirmation. The labels were translated and the answers were not.
#
# There was one recorded reason for leaving them: routing a toast through t() used to drop it out
# of tools/message_report.py's house-style check. That stopped being true when message_report was
# taught to resolve t("key") back to its English sentence and lint THAT, and the decision was never
# revisited. So they are being converted, and this number is the count of what is left.
#
# A build fails if the count goes UP. Lower it whenever you convert some; never raise it.
RAW_DIALOG_CEILING = 19

DIALOG_CALLS = ("toast", "toastHtml", "confirm", "window.confirm", "prompt")


def raw_dialog_strings(s):
    """Dialog calls whose message is still a raw English literal rather than t()/tsub().

    Counts the CALL, not the sentence: one toast built from two literals is one thing a reader
    sees. A call whose argument is only a variable is not counted - there is no English in it.
    """
    import re as _re
    hits = []
    pat = _re.compile(r"\b(" + "|".join(c.replace(".", r"\.") for c in DIALOG_CALLS) + r")\s*\(")
    for m in pat.finditer(s):
        arg = _first_arg(s, m.end() - 1)
        lits = _re.findall(r'"((?:[^"\\]|\\.)*)"', arg)
        # A literal that is the key inside t("...")/tsub("...") is already translated.
        keyed = set(_re.findall(r'\bt(?:sub)?\(\s*"((?:[^"\\]|\\.)*)"', arg))
        english = [x for x in lits if x not in keyed and _re.search(r"[A-Za-z]{3}", x)]
        if english:
            hits.append((s.count("\n", 0, m.start()) + 1, m.group(1), english[0][:70]))
    return hits


def hardcoded_markup(s):
    """Rough count of visible text in the static markup with no data-i18n on its element."""
    # The FIRST <script> sits in <head>, long before <body> - slicing to it produced an empty
    # string and a confident, wrong "0". Take from <body> to the LAST <script>, which is the one
    # big UI block at the end of the file.
    b = s.find("<body")
    e = s.rfind("<script")
    body = s[b:e] if (b >= 0 and e > b) else ""
    n = 0
    for m in re.finditer(r">([^<>{}]{3,60})<", body):
        txt = m.group(1).strip()
        if not txt or not re.search(r"[A-Za-z]", txt):
            continue
        if re.match(r"^[\s\d.,:/\\|-]+$", txt):
            continue
        n += 1
    return n


def main():
    check = "--check" in sys.argv
    s = read()
    block = dict_block(s)
    if not block:
        print("i18n_report: could not find the I18N dictionary")
        return 1
    langs = languages(block)
    if "en" not in langs:
        print("i18n_report: no English dictionary")
        return 1

    used = used_keys(s)
    defined_en = langs["en"]
    undefined = sorted(used - defined_en)
    unused = sorted(defined_en - used)

    print("languages : %s" % ", ".join(sorted(langs)))
    print("keys (en) : %d      used by the app: %d" % (len(defined_en), len(used)))
    print()

    # COVERAGE IS MEASURED AGAINST THE KEYS THE APP ACTUALLY USES, not every key defined.
    # The dictionary carries keys nothing asks for yet; translating those is work no one can see,
    # and counting them made a fully-translated language read as 42% complete.
    target = used & defined_en
    worst = 0
    for code in sorted(langs):
        if code == "en":
            continue
        missing = sorted(target - langs[code])
        worst = max(worst, len(missing))
        pct = 100.0 * (len(target) - len(missing)) / max(len(target), 1)
        flag = "OK " if not missing else "GAP"
        print("  [%s] %-3s %6.1f%% of the %d live keys   missing %d%s"
              % (flag, code, pct, len(target), len(missing),
                 ("  e.g. " + ", ".join(missing[:4])) if missing else ""))

    print()
    if undefined:
        print("  [BAD] %d key(s) USED but not defined - these render as the key itself:" % len(undefined))
        for k in undefined[:20]:
            print("        %s" % k)
    else:
        print("  [OK ] every key the app uses is defined")

    if unused:
        print("  [ .. ] %d defined key(s) never used (dead weight, not an error)" % len(unused))

    # THE BACKLOG THE COVERAGE FIGURE ABOVE CANNOT SHOW. Reported, never failed.
    #
    # Coverage is measured against the keys the app USES, and that is deliberate - 3.60.0 tried
    # measuring against every DEFINED key and eight fully-translated languages read as 42.3%, so it
    # was reversed. The side effect is that a language can print 100% while being short of English
    # by every key that is not wired up yet, and those are precisely the keys the next UI change
    # reaches for: wiring one turns a green gate red with no warning that it was coming.
    #
    # This is that warning. It is information, so it can never block a build over work no user can
    # see - but nobody can now be surprised by it either.
    short = [(code, len(defined_en - langs[code])) for code in sorted(langs)
             if code != "en" and (defined_en - langs[code])]
    if short:
        print("  [ .. ] dictionary backlog (NOT a failure): %d language(s) carry fewer keys than en"
              % len(short))
        for code, n in short:
            print("         %-3s %d of %d defined, %d not written yet"
                  % (code, len(langs[code]), len(defined_en), n))
        print("         wiring any of those into t()/tsub()/data-i18n makes --check FAIL")

    hard = hardcoded_markup(s)
    print("  [ .. ] roughly %d visible text nodes in the static markup (hint only, not a failure)" % hard)

    raw = raw_dialog_strings(s)
    if len(raw) > RAW_DIALOG_CEILING:
        print("\n  [FAIL] %d dialog call(s) still carry raw English - the ceiling is %d"
              % (len(raw), RAW_DIALOG_CEILING))
        for line, fn, txt in raw[:12]:
            print("         %-5d %-14s %s" % (line, fn, txt))
    elif len(raw) < RAW_DIALOG_CEILING:
        print("  [ .. ] %d dialog call(s) still carry raw English - below the ceiling of %d, "
              "lower RAW_DIALOG_CEILING to %d" % (len(raw), RAW_DIALOG_CEILING, len(raw)))
    else:
        print("  [ .. ] %d dialog call(s) still carry raw English (at the ceiling)" % len(raw))

    if check:
        if len(raw) > RAW_DIALOG_CEILING:
            print("\nFAIL: more dialog strings are hardcoded than before. They cannot be translated.")
            return 1
        if undefined:
            print("\nFAIL: keys used but not defined.")
            return 1
        if worst:
            print("\nFAIL: %d live key(s) missing from at least one language." % worst)
            return 1
        print("\nOK: every language covers all %d keys the app uses." % len(target))
    return 0


if __name__ == "__main__":
    sys.exit(main())

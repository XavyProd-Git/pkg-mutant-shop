# -*- coding: utf-8 -*-
"""Keep ps4-app/onconsole/cheat_core.h honest against its source in ps5-app/onconsole/server.c.

WHAT THIS COPIES, AND WHY IT IS A COPY. The PS5's cheat engine divides cleanly in two. Underneath
are mem_read/mem_write, which reach a running game's memory, and those are entirely PS5 - a kernel
page-table walk that has no PS4 equivalent (the PS4 reaches memory through an in-game agent
instead; see ps4-app/plugin/). Above them is everything else: reading a cheat file in any of our
three formats, decrypting .mc4, converting Trainer XML, picking the right file for a title and
version, computing an address, deciding whether a mod is currently on, applying and reverting it,
and the whole game-patch engine. Not one line of that is platform-specific. It is byte logic.

So the PS4 does not get a second implementation of it - it gets THIS one. Copied, because the PS5
file is shipping and editing it to share code is all downside (the same judgement sqmini.h was
made under, and for the same reason). Copied by a script rather than by hand, because a hand copy
rots silently: the PS5 gains a fix, the PS4 keeps the bug, and nothing says so.

    python tools/ps4_sync_cheat_core.py            # regenerate the copy from server.c
    python tools/ps4_sync_cheat_core.py --check    # exit 1 if it has drifted (both ELF builds)

WHAT THE PS4 MUST STILL PROVIDE ITSELF, and the list is short on purpose:
    mem_read / mem_write / running_game   - the agent client in server_ps4.c
    CHEAT_ROOT and friends                - where its library lives
    json_escape, now_ms, and the usual small helpers it already had
If a name goes missing from that list the PS4 build fails to link, loudly, which is the point.
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SRC = os.path.join(ROOT, "ps5-app", "onconsole", "server.c")
DST = os.path.join(ROOT, "ps4-app", "onconsole", "cheat_core.h")

# Dependency order matters: this is emitted as one header with no forward declarations of its own
# beyond the block below, so a function must appear after everything it calls that is also here.
FUNCS = [
    # --- AES-256-CBC, for .mc4 ---
    "aes_build_tables", "aes_gmul", "aes256_expand", "aes_add_round_key",
    "aes_inv_shift_rows", "aes_inv_sub_bytes", "aes_inv_mix_columns",
    "aes256_decrypt_block", "aes256_cbc_decrypt",
    # --- base64 + the text buffer the converters build into ---
    "b64val", "b64_decode",
    "tbuf_need", "tbuf_putn", "tbuf_puts", "tbuf_putc", "tbuf_put_json", "tbuf_put_hex",
    # --- Trainer XML (.shn) and the encrypted form (.mc4) -> our cheat JSON ---
    "hex2bytes", "patch_unescape", "isalnum_c", "xml_attr", "xml_child", "tbuf_put_json_xml",
    "shn_xml_to_json", "mc4_to_xml",
    # --- loading a cheat document whatever its format ---
    "path_ext_is", "cheat_load_doc",
    # --- the library on disk: what is there, and which file fits this title ---
    "file_exists", "cheat_sniff", "cheat_pick_file", "cheat_versions_json",
    # Taking delivery of a cheat file: the FTP drop folder, a USB stick, the Rescan button. This
    # is file logic, not console logic, and the PS4 had none of it - its rescan route answered
    # "not implemented on the PS4 yet", which is also what the Settings button called.
    "count_dir", "copy_file", "move_file", "cheat_intake_patch", "cheat_intake_dir",
    "cheat_intake_all",
    "notify_cheats_filed",
    # --- walking the document ---
    # NOT slurp(): server_ps4.c has had its own since it was written, and a second copy is a
    # redefinition rather than a port. Same for json_escape, now_ms and mkparents.
    "json_str_after_lim", "json_str_after", "json_num_after", "mods_array_start", "next_mod_block", "find_mod_block",
    "mod_dropped_count", "parse_mod_entries_ex", "parse_mod_entries",
    # --- addresses, state, apply/revert ---
    "entry_discriminating", "cheat_addr_mode", "cheat_entry_addr",
    "cheat_mod_state_blk", "offset_known_state",
    # The master code ("Master Code 1 (Must Be On)"): a shared routine the cheats in the same file
    # are diffs against. cheat_apply_blk calls it, so it has to come first.
    # The signature search: the primitive porting, the mask patch lines and "find an address from
    # scratch" all need. Read-only, one job at a time, in its own thread.
    "sig_now_ms", "sig_parse", "sig_thread", "sig_start", "sig_cancel", "sig_status_json",
    "rip_disp32", "rip_targets", "run_unreachable",
    "cheat_master_span", "cheat_master_release", "master_hook_cave", "master_cave_is_ours",
    "bytes_appear_in",
    "cheat_master_decide", "cheat_master_commit", "master_overlay", "master_covers", "master_why_text",
    "cheat_master_off", "cheat_master_info", "cheat_apply_blk",
    "cheat_rc_message",
    "cheat_result_toast", "cheat_apply_mod_doc", "cheat_apply_mod",
    # --- game patches (the XML library) ---
    "patch_file_for", "patch_count", "patch_block", "patch_open_tag",
    "patch_encode_value", "patch_parse_lines", "patches_json",
    "patch_meta_attr", "patch_result_toast",
    # patch_undo_write and undo_read_exact came in when the undo record stopped using stdio -
    # fopen does not work in the PS4 payload, so Revert could never find what Apply saved.
    "patch_undo_path", "patch_undo_open", "patch_undo_write", "undo_read_exact",
    "patch_apply", "patch_revert",
    # NOT patch_action_json: it is a response builder written against the PS5's
    # title_t/read_console_titles, and the PS4 has its own row type. server_ps4.c
    # writes that one itself rather than have this header depend on a name that
    # differs between the two payloads.
]

# Whole regions lifted verbatim: the types and limits the functions above are written against.
# (first line marker, last line marker) - both matched as a prefix of a stripped line.
REGIONS = [
    # Where a cheat library can live. LEGACY_CHEAT_ROOT is read by cheat_pick_file when looking for
    # a file an older install left behind.
    ("#define LEGACY_CHEAT_ROOT", "#define LEGACY_CHEAT_ROOT"),
    # The AES lookup tables and their built-once flag. Extracted as a region because they are file
    # statics rather than functions, and every AES routine below reads them.
    ("static uint8_t aes_sbox_tbl[256], aes_rsbox_tbl[256];", "static int aes_tables_ready = 0;"),
    # The cap on one Trainer-XML chunk.
    ("#define SHN_CHUNK_MAX", "#define SHN_CHUNK_MAX"),
    # The copy buffer the intake reads through. A define rather than a literal because it is sized
    # against a worker thread's stack on both consoles - see copy_file's own comment.
    ("#define COPY_BUF_BYTES", "#define COPY_BUF_BYTES"),
    # The game-patch limits and the line type. PATCH_NO_ASLR is 0x400000 - the PS4/PS5 no-ASLR load
    # address a patch's addresses are written against, which is also the PS4's real image base.
    ("#define PATCH_NO_ASLR", "} patch_line_t;"),
    ("#define PATCH_LINES_BYTES", "#define PATCH_LINES_BYTES"),
    # Where an applied patch's original bytes are kept so revert has something to put back.
    ("#define PATCH_UNDO_DIR", "#define PATCH_UNDO_DIR"),
    # The sizing block, the entry type, the address sanity macro and the heap-size macro. The
    # comment above CHEAT_MAX_MODS records what each limit was measured against and is worth more
    # than the numbers - so it is carried across with them.
    ("#define CHEAT_MAX_MODS", "#define CHEAT_ENTS_BYTES"),
    ("typedef struct { char *buf; size_t len, cap; } tbuf_t;",
     "typedef struct { char *buf; size_t len, cap; } tbuf_t;"),
    # The RIP-relative prefix table and its cap. rip_targets() reads them, and they are a measurement
    # of what real cheat files contain rather than a general decoder - the comment above them says so
    # and travels with them.
    # The signature type, its limits and the job the thread works through. Types and file statics, so
    # they cannot be emitted as functions.
    ("#define SIG_MAX", "typedef struct pms_sig {"),
    # The cap on one sweep. A define next to the function that enforces it.
    ("#define SIG_SPAN_MAX", "#define SIG_SPAN_MAX"),
    ("static struct sig_job {", "} g_sig = "),
    ("#define RIP_MAX_OPS", "};"),
    # Why a master code was refused, and the plan the decision is recorded in. Both are types the
    # functions below are written against, so they cannot be emitted as functions.
    ("enum {                                   /* why a master was refused", "};"),
    ("typedef struct cheat_master_plan {", "} cheat_master_plan_t;"),
]


_IN_BLOCK_COMMENT = [False]


def count_braces(line):
    """Braces that are really braces: not the ones inside a char or string literal or a comment.

    This is not fussiness. next_mod_block() - the function that walks a cheat document's mod
    array - is written in terms of the characters '{' and '}', and counting those as structure
    made its brace depth never return to zero, so the extractor reported it missing. Every other
    function that quotes a brace would have been mis-extracted the same way and, being merely
    truncated rather than absent, would have done it SILENTLY. A block comment spanning lines is
    tracked across calls, which is why this keeps state.
    """
    o = c = 0
    i, n = 0, len(line)
    in_block = _IN_BLOCK_COMMENT[0]
    while i < n:
        ch = line[i]
        if in_block:
            if ch == "*" and i + 1 < n and line[i + 1] == "/":
                in_block = False
                i += 2
                continue
            i += 1
            continue
        if ch == "/" and i + 1 < n and line[i + 1] == "/":
            break                                    # rest of the line is a comment
        if ch == "/" and i + 1 < n and line[i + 1] == "*":
            in_block = True
            i += 2
            continue
        if ch in "'\"":
            q = ch
            i += 1
            while i < n:
                if line[i] == "\\":
                    i += 2
                    continue
                if line[i] == q:
                    i += 1
                    break
                i += 1
            continue
        if ch == "{":
            o += 1
        elif ch == "}":
            c += 1
        i += 1
    _IN_BLOCK_COMMENT[0] = in_block
    return o, c


def defn(lines, name):
    """The full text of one top-level definition of `name`, matched by brace depth.

    Matched on the DEFINITION - a line that both mentions the name followed by '(' and is not a
    forward declaration (those end in ';'). Multi-line signatures are handled because the brace
    counter only starts at the first '{'.
    """
    # The return type may be several words, and its pointer star binds to the NAME rather than the
    # type - `static const char *next_mod_block(`. The first version of this pattern did not allow
    # a '*' before the name and so found 55 of 58 functions and silently missed that one shape.
    pat = re.compile(r"^(?:static\s+|const\s+|inline\s+)*[A-Za-z_][\w \t\*]*?\**%s\s*\("
                     % re.escape(name))
    for i, l in enumerate(lines):
        if not pat.match(l):
            continue
        # Walk forward to the opening brace, refusing anything that terminates first.
        j, brace = i, -1
        while j < len(lines) and j < i + 12:
            if ";" in lines[j] and "{" not in lines[j]:
                break                     # a forward declaration - keep looking
            if "{" in lines[j]:
                brace = j
                break
            j += 1
        if brace < 0:
            continue
        depth, k = 0, brace
        started = False
        _IN_BLOCK_COMMENT[0] = False          # each function scanned from a clean comment state
        while k < len(lines):
            o, c = count_braces(lines[k])
            depth += o - c
            if o:
                started = True
            if started and depth <= 0:
                # Carry the comment block immediately above, which is where the reasoning lives.
                top = i
                while top > 0 and (lines[top - 1].lstrip().startswith("*")
                                   or lines[top - 1].lstrip().startswith("/*")
                                   or lines[top - 1].lstrip().startswith("//")):
                    top -= 1
                return "\n".join(lines[top:k + 1])
            k += 1
        break
    return None


def region(lines, first, last):
    for i, l in enumerate(lines):
        if l.strip().startswith(first):
            for j in range(i, min(i + 60, len(lines))):
                if lines[j].strip().startswith(last):
                    return "\n".join(lines[i:j + 1])
    return None


def build():
    lines = io.open(SRC, encoding="utf-8", errors="replace").read().split("\n")
    out, missing = [], []

    out.append("""/* cheat_core.h - the PS4's copy of the PS5 cheat engine's platform-neutral half.
 *
 * GENERATED by tools/ps4_sync_cheat_core.py from ps5-app/onconsole/server.c. DO NOT EDIT: the next
 * sync overwrites it, and a fix made here would be a fix the PS5 never gets. Change server.c and
 * re-run the tool. Both ELF builds run it with --check, so the two cannot drift apart quietly.
 *
 * Everything in here is byte logic - parsing our three cheat formats, decrypting .mc4, choosing
 * the file that matches a title and version, computing addresses, deciding whether a mod is on,
 * applying and reverting, and the game-patch engine. It reaches memory only through mem_read and
 * mem_write, which each console defines for itself: a kernel page-table walk on the PS5, and on
 * the PS4 a small agent GoldHEN loads into the game (mdbg and ptrace are both EPERM from our
 * payload there - measured, twice, on 13.52).
 *
 * The PS4 must define, BEFORE including this: mem_read, mem_write, running_game, json_escape,
 * and its CHEAT_*_DIR paths. Anything missing is a link error, which is the intended failure.
 */
#pragma once
""")

    for first, last in REGIONS:
        t = region(lines, first, last)
        if t is None:
            missing.append("region:%s" % first[:40])
        else:
            out.append(t)
            out.append("")

    for fn in FUNCS:
        t = defn(lines, fn)
        if t is None:
            missing.append(fn)
        else:
            out.append(t)
            out.append("")

    return "\n".join(out), missing


def main():
    check = "--check" in sys.argv
    text, missing = build()
    if missing:
        print("ps4_sync_cheat_core: NOT FOUND in server.c: %s" % ", ".join(missing))
        return 1
    have = io.open(DST, encoding="utf-8").read() if os.path.exists(DST) else ""
    if check:
        if have != text:
            print("ps4_sync_cheat_core: cheat_core.h has DRIFTED from server.c - "
                  "run tools/ps4_sync_cheat_core.py")
            return 1
        print("ps4_sync_cheat_core: in sync (%d functions)" % len(FUNCS))
        return 0
    io.open(DST, "w", encoding="utf-8", newline="\n").write(text)
    print("ps4_sync_cheat_core: wrote %s (%d functions, %d lines)"
          % (os.path.relpath(DST, ROOT), len(FUNCS), text.count("\n") + 1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env bash
# Build pms-agent.prx - the in-game half of the PS4 cheat engine.
#
# WHY THIS IS NOT GoldHEN's MAKEFILE. Its plugin Makefile links $(GOLDHEN_SDK)/build/crtprx.o and
# -lGoldHEN_Hook. We need neither: the hook library is for patching other people's functions, which
# this agent deliberately does not do, and OpenOrbis ships its own library startup object
# (lib/crtlib.o) for exactly this shape of output. That matters beyond tidiness - the GoldHEN SDK
# repository would not clone on this machine (git timed out twice), and a build that depends on it
# is a build nobody can reproduce here. Everything below comes out of the OpenOrbis toolchain that
# is checked out locally, plus the ONE fact that had to come from GoldHEN: how its loader finds a
# plugin, which is dlsym("plugin_load") - and that was read off the console's own plugin_loader.prx.
#
# The link line, the --paid value and the create-fself --lib form are GoldHEN's, read from
# plugin_src/plugin_template/Makefile in its plugins repository (which did clone). They are copied
# rather than invented because a PRX the loader will not accept fails silently.
#
# Usage (WSL):  bash ps4-app/plugin/build-wsl.sh
set -e

HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

export OO_PS4_TOOLCHAIN="${OO_PS4_TOOLCHAIN:-$HOME/ps4tool/OpenOrbis/OpenOrbis/PS4Toolchain}"
T="$OO_PS4_TOOLCHAIN"
CLANGBIN="${CLANGBIN:-$HOME/clang18/usr/lib/llvm-18/bin}"

if [ ! -d "$T" ]; then
  echo "ABORT: OpenOrbis toolchain not found at $T" >&2
  exit 1
fi
if [ ! -f "$T/lib/crtlib.o" ]; then
  echo "ABORT: $T/lib/crtlib.o is missing - this toolchain cannot build a library." >&2
  exit 1
fi

CC="$CLANGBIN/clang"
LD="$CLANGBIN/ld.lld"
[ -x "$CC" ] || CC="$(command -v clang || true)"
[ -x "$LD" ] || LD="$(command -v ld.lld || true)"
if [ ! -x "$CC" ] || [ ! -x "$LD" ]; then
  echo "ABORT: need clang and ld.lld (looked in $CLANGBIN and on PATH)" >&2
  exit 1
fi

OUT="$HERE/build"
mkdir -p "$OUT"

# EACH VARIANT OWNS ITS OWN BASENAME, and this is a scar. ps4-app/onconsole/agent_bundle.h does
# AB_INCBIN(ab_pms_agent_prx, "../plugin/build/pms-agent.prx"), and server_ps4.c includes it - so the
# PS4 ELF embeds whatever is at that exact path, and the exe ships that ELF. When a diagnostic build
# wrote to pms-agent.prx, every PS4 ELF built afterwards silently carried the diagnostic instead of
# the agent. A variant now writes pms-agent-<variant>.prx and cannot touch the shipping name at all.
# (The PS5 ELF does NOT embed it - only server_ps4.c includes agent_bundle.h.)
NAME="pms-agent"

# freebsd12 target, PIC, no host headers - the same flags GoldHEN's plugins compile with.
CFLAGS="--target=x86_64-pc-freebsd12-elf -fPIC -funwind-tables -c -Wall -O2 \
  -isysroot $T -isystem $T/include"

# PMS_AGENT_TRACE=1 (env) compiles in the load-path breadcrumbs (source/main.c). A diagnostic aid
# only: it writes one short line per step to /data/pkg-mutant-shop/agent-trace.txt so that if a
# game ever freezes at load, the file shows the last step reached. Off by default - production and
# the PC test build carry no file I/O in the agent at all.
# PMS_AGENT_MINIMAL=1 builds the decisive experiment described in source/main.c: plugin_load
# leaves one breadcrumb and returns, with no thread and no other work at all. It tells us whether
# the module itself can load, which no amount of editing our own logic can answer.
if [ "${PMS_AGENT_MINIMAL:-0}" = "1" ]; then
  CFLAGS="$CFLAGS -DPMS_AGENT_MINIMAL"
  NAME="pms-agent-minimal"
  echo "== NOTE: building the MINIMAL diagnostic agent (plugin_load only) =="
  echo "==       output is $NAME.prx - the shipping pms-agent.prx is left alone =="
fi
if [ "${PMS_AGENT_NULL:-0}" = "1" ]; then
  CFLAGS="$CFLAGS -DPMS_AGENT_NULL"
  NAME="pms-agent-nulltest"
  echo "== NOTE: building the ZERO-IMPORT agent (plugin_load returns 0, calls nothing) =="
  echo "==       output is $NAME.prx - the shipping pms-agent.prx is left alone =="
fi
if [ "${PMS_AGENT_TRACE:-0}" = "1" ]; then
  CFLAGS="$CFLAGS -DPMS_AGENT_TRACE"
  echo "== NOTE: building agent WITH load-path breadcrumbs (PMS_AGENT_TRACE) =="
fi

rm -f "$OUT"/*.o "$OUT/$NAME.elf" "$OUT/$NAME.oelf" "$OUT/$NAME.prx"

echo "== compile =="
"$CC" $CFLAGS -o "$OUT/main.o" "$HERE/source/main.c"

# OUR OWN crt, not OpenOrbis's crtlib.o. See source/crt_prx.c for the full why: crtlib.o is the
# startup for a standalone homebrew ELF and runs the whole C runtime at load AND stamps the module
# with SDK 0x1000051; a plugin the loader maps into a running game must do nothing at load and carry
# the SDK version the working plugins on this firmware carry (0x4508101). Building our plugin with
# crtlib.o was the difference between it and GoldHEN's plugins that load cleanly, and load-time is
# where the game froze. Our crt is trivial: the right sce_module_param, and _init -> module_start
# (which returns 0). Nothing of ours runs until the loader calls plugin_load.
echo "== compile our crt =="
"$CC" $CFLAGS -o "$OUT/crt_prx.o" "$HERE/source/crt_prx.c"

# -e _init is load-bearing: a PRX is entered through _init, not main, and without it the module
# starts at the wrong place and the loader reports success while nothing runs.
#
# --pack-dyn-relocs=none: CHEAP INSURANCE, AND NOT THE CAUSE. Written down honestly because the
# first version of this note claimed it WAS the cause, and that was wrong.
#
# A dump of our DYNAMIC table appeared to show an extra leading entry, tag 0x24 (DT_RELR, the
# compact relative-relocation format lld 18 emits by default) that neither working plugin had. It
# was a MISREAD: the 16 bytes before the table are the tail of a preceding array of consecutive
# indices, and a scanner looking for "plausible tags" swallowed them. Both tables really begin at
# tag 0x61000025 and both hold 37 identical entries in the same order. There is no DT_RELR in our
# module and there never was. Verify the instrument before believing the bug.
#
# The flag stays only because it costs nothing and guarantees this old loader is never handed a
# relocation format it predates. It changed nothing and it fixed nothing.
#
# EXPORT EXACTLY WHAT THE LOADER LOOKS UP - not every symbol in the module.
#
# A blanket --export-dynamic is what we used to pass, and it is a real, measured difference from
# every plugin this console loads cleanly: those are built WITHOUT it. It exports every
# default-visibility symbol - all of our own functions plus everything libc-facing - and our export
# data came out noticeably larger than the template's for a module of similar size. A loader
# walking a far longer export table than any plugin it was written against is a plausible way to
# break a load, and it is the last real deviation left now that the crt, the paid, the SDK version,
# the segment set and the DYNAMIC table have all been matched.
#
# module_start / module_stop are NOT in that list, and putting them there was a mistake worth
# recording: both are declared attr_module_hidden (weak + hidden visibility), exactly as the
# working plugins declare them, and the loader never looks them up - our crt calls module_start
# directly. Asking the linker to export a symbol that is deliberately hidden is contradictory, and
# a module that crashes games at load is not where you keep a contradiction.
#
# Naming the remaining symbols keeps plugin_load findable - removing the flag entirely leaves it
# unexported, which the build check below catches - while producing a table the size a plugin is
# supposed to have.
#
# The original note, kept because it is why a blanket flag was there at all: GoldHEN's own
# plugin Makefile does not pass it, so it was removed to match that proven recipe exactly - and the
# module check immediately refused the build: without it our plugin_load is not exported at all,
# the name is not even in the file. Their plugins get the export some other way (their SDK lib or
# crt keeps the symbols alive); ours needs the flag. Removing it fails SAFE - a plugin the loader
# cannot find does nothing - but it is also a useless plugin, so the flag stays.
#
# Measured while chasing a plugin that breaks games, which is why this note is long: THE BUILD
# RECIPE IS NOT THE DIFFERENCE. Their CFLAGS are identical to ours (same freebsd12 target, same
# -fPIC -funwind-tables -isysroot), the --paid is the same, the crt is now equivalent, and the
# module we produce matches a working one on segment set and DYNAMIC size. Look elsewhere.
#
# The original note, kept because it is why the flag was added:
# --export-dynamic was load-bearing, and it cost a build to find. Nothing inside this module
# references plugin_load - the only caller is the plugin loader, from outside, by dlsym - so the
# linker had every reason to leave it out of the dynamic table, and it did: the first build
# produced a clean .prx whose dynamic symbol table was EMPTY. visibility("default") is not enough
# on its own for a -pie link; it says "may be exported", not "export it". A plugin like that loads
# without complaint and then does nothing at all, which on a console is indistinguishable from
# never having been listed in plugins.ini.
echo "== link =="
"$LD" "$OUT/crt_prx.o" "$OUT/main.o" -o "$OUT/$NAME.elf" \
  -m elf_x86_64 -pie --script "$T/link.x" -e _init --eh-frame-hdr \
  --export-dynamic-symbol=plugin_load --export-dynamic-symbol=plugin_unload \
  --export-dynamic-symbol=g_pluginName --export-dynamic-symbol=g_pluginDesc \
  --export-dynamic-symbol=g_pluginAuth --export-dynamic-symbol=g_pluginVersion \
  --pack-dyn-relocs=none \
  -L"$T/lib" -lSceLibcInternal -lkernel -lSceSysmodule

# WHAT THE LOADER IS ASKED TO MAP, printed on every build. A module's memory image can legitimately
# exceed its file image - that is what .bss is - but ours used to ask for 0x1c1dc bytes beyond the
# file for two 64 KiB scratch buffers on a path that never moves more than 256 bytes, while the 12
# plugins this console loads cleanly all keep .bss under about 1.1 KB. That was not the cause of our
# load failure (the MINIMAL build has no .bss at all and still broke the game), so this is not a
# fix wearing a gate - it is a number worth seeing, and a ceiling so a stray big static inside
# somebody's game has to be deliberate.
echo "== loaded image =="
BSS_HEX="$(readelf -SW "$OUT/$NAME.elf" | awk '$2==".bss"{print $6; exit}')"
[ -n "$BSS_HEX" ] || BSS_HEX=0
BSS_SZ=$((16#$BSS_HEX))
echo "  .bss: $BSS_SZ byte(s)"
readelf -lW "$OUT/$NAME.elf" | awk '/LOAD/{print "  LOAD filesz=" $5 " memsz=" $6 " " $7}'
if [ "$BSS_SZ" -gt 20000 ]; then
  echo "ABORT: .bss is $BSS_SZ bytes (ceiling 20000)." >&2
  echo "       This module is mapped into somebody's game. A large zero-filled region is memory the" >&2
  echo "       game does not get back, and every plugin this console loads keeps it under ~1.1 KB." >&2
  echo "       Shrink the static buffers (see PMS_MAX_CHUNK in source/main.c) or raise this on purpose." >&2
  exit 1
fi

# THE IMPORT SET, PINNED. An import the game's libraries cannot resolve fails the module load, and
# this module's load failure is still unexplained - so what it imports is the variable under
# isolation and must never move without somebody deciding to move it. It moved once already: a
# one-byte fwrite() of a newline was compiled into fputc(). Only the default build is checked; the
# diagnostic variants exist precisely to have different import sets.
if [ "$NAME" = "pms-agent" ]; then
  echo "== imports =="
  readelf --dyn-syms -W "$OUT/$NAME.elf" | awk '$7=="UND" && $8!="" {print $8}' | sort -u > "$OUT/imports.actual"
  grep -v "^#" "$HERE/imports.txt" | grep -v "^$" | sort -u > "$OUT/imports.expected"
  if ! diff -u "$OUT/imports.expected" "$OUT/imports.actual" > "$OUT/imports.diff"; then
    echo "ABORT: the in-game module's import set changed." >&2
    echo "       An import the game cannot resolve fails the module load, and this module's load" >&2
    echo "       failure is still unexplained - so this list does not move by accident." >&2
    echo "       - is expected (ps4-app/plugin/imports.txt), + is what this build produced:" >&2
    sed "s/^/       /" "$OUT/imports.diff" >&2
    echo "       If the change is deliberate, edit imports.txt in the same commit and say why." >&2
    exit 1
  fi
  echo "  ok: $(wc -l < "$OUT/imports.actual") import(s), exactly as pinned"
fi

echo "== fself (--lib makes it a .prx the loader will accept) =="
"$T/bin/linux/create-fself" \
  -in="$OUT/$NAME.elf" \
  -out="$OUT/$NAME.oelf" \
  --lib="$OUT/$NAME.prx" \
  --paid 0x3800000000000011   --sdkver 0x4508101

if [ ! -s "$OUT/$NAME.prx" ]; then
  echo "ABORT: no .prx was produced." >&2
  exit 1
fi

# --sdkver 0x4508101 IS THE RIGHT WAY TO SET IT, and it is set at SIGNING time so the module stays
# internally consistent. Our plugin was declaring SDK 0x1000051 (create-fself's own default, via
# its RewriteSDKVersion) while EVERY plugin that loads cleanly on this console declares 0x4508101 -
# measured by reading game_patch.prx and plugin_template.prx off the console. That field is exactly
# the kind of thing a module loader validates, and it was the last concrete difference left between
# our module and a working one.
#
# THE SDK VERSION IS NOT PATCHED AFTER SIGNING - and this is a scar, not a preference.
#
# create-fself rewrites the module-param SDK version to OpenOrbis's 0x1000051, discarding the
# 0x4508101 our crt puts in the .elf. A previous build "fixed" that by patching those 8 bytes in
# the .prx AFTER create-fself had signed it. Do not do that: the .prx is a fake-SIGNED module, and
# editing its bytes after signing hands the loader something structurally inconsistent. The console
# did not merely hang on that build - it had to be rebooted and re-jailbroken.
#
# If the SDK version ever genuinely needs to be 0x4508101, it has to come from the signer, never
# from a patch applied to its output.

# The loader dlsyms these two by name. If they are not in the dynamic table the plugin loads and
# does nothing, which looks exactly like a plugin that was never listed - so it is checked here
# rather than discovered on a console.
echo "== exported entry points =="
for sym in plugin_load plugin_unload; do
  if strings -a "$OUT/$NAME.prx" | grep -qx "$sym"; then
    echo "  ok: $sym"
  else
    echo "ABORT: $sym is not exported - GoldHEN's loader would find nothing." >&2
    exit 1
  fi
done

ls -la "$OUT/$NAME.prx"
echo "BUILT: $OUT/$NAME.prx"

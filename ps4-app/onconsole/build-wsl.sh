#!/usr/bin/env bash
# Build the PS4 on-console payload (the PS4 half of PKG MUTANT SHOP).
#
# This is the sibling of ps5-app/onconsole/build-wsl.sh and deliberately runs THE SAME GATES: the
# two payloads share one web UI, and a UI that will not parse, a language missing a key, or a
# message off house style must stop both builds, not just one. It does NOT touch the PS5 tree.
#
# One-time setup this performs by itself if needed: clone the ps4-payload-dev SDK and apply
# sdk-goldhen.patch.py to it. That patch is what makes a payload actually RUN under GoldHEN - see
# the comments in the patch for exactly why, and never skip it.
#
# TWO BUILDS COME OUT OF THIS FILE:
#   (default)  the full payload - carries the home-screen app's package and installs it
#   --lite     the same shop with NO package inside, which is the copy that travels INSIDE that
#              package so pressing the icon can start the shop
# That is how the cycle is broken. ps4-app/build-all-wsl.sh builds them in the only order that
# works; running this on its own builds the full one and needs the package to exist already.
#
# Usage (WSL):  bash build-wsl.sh [--lite] [PORT]
set -e
LITE=0
if [ "${1:-}" = "--lite" ]; then LITE=1; shift; fi
PORT="${1:-8710}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

SDK="$HOME/sdk/ps4-payload-sdk"
SDK_SRC="$HOME/sdk/ps4-payload-sdk.tmp"
export PS4_PAYLOAD_SDK="$SDK"
export PATH="$HOME/bin:$PATH"

# The SDK's makefiles ask llvm-config for --bindir. This box has clang-18 unpacked without
# llvm-config, so provide the one answer it wants rather than installing a toolchain.
if ! command -v llvm-config >/dev/null 2>&1; then
  mkdir -p "$HOME/bin"
  CLANGBIN="$HOME/clang18/usr/lib/llvm-18/bin"
  printf '#!/bin/sh\ncase "$1" in\n  --bindir) echo "%s" ;;\n  --version) echo "18.1.3" ;;\n  *) echo "" ;;\nesac\n' "$CLANGBIN" > "$HOME/bin/llvm-config"
  chmod +x "$HOME/bin/llvm-config"
fi

if [ ! -d "$SDK_SRC" ]; then
  echo "== fetching the ps4-payload-dev SDK (one time)"
  mkdir -p "$HOME/sdk"
  git clone --depth 1 https://github.com/ps4-payload-dev/sdk.git "$SDK_SRC"
fi

echo "== patching the SDK crt for GoldHEN (idempotent)"
python3 "$HERE/sdk-goldhen.patch.py" "$SDK_SRC"

if [ ! -x "$SDK/bin/orbis-clang" ] || [ "$SDK_SRC/crt/crt.c" -nt "$SDK/target/lib/crt1.o" ]; then
  echo "== building/installing the SDK"
  ( cd "$SDK_SRC" && make DESTDIR="$SDK" install >/dev/null )
fi

TOOLS="$HERE/../../tools"
WEB="$HERE/../../web"

# THE SHARED GATES. Both payloads embed the same web/ directory, so a broken UI must stop this
# build too - it is the single point of failure the PS5 side already learned to gate.
python3 "$TOOLS/check_web.py"      || { echo "ABORT: the UI script is broken - not building." >&2; exit 1; }
python3 "$TOOLS/i18n_report.py" --check || { echo "ABORT: a language is missing keys the app uses." >&2; exit 1; }
python3 "$TOOLS/message_report.py" --check || { echo "ABORT: a user-facing message is off house style." >&2; exit 1; }

# A PS5 game must not offer a PS4 tab in its mods section. The rule lives in one line of
# modsTabConsoles(), which defers to eligibleConsoles() - so an edit to EITHER can undo it,
# and the symptom only appears on a two-console fleet, for one class of title.
python3 "$TOOLS/test_mods_tabs.py" || { echo "ABORT: the mods console tabs offer the wrong consoles." >&2; exit 1; }

# The panel installs onto ONE console, so every fact it prints must be that console's. An update
# already on the PS5 read as "Installed" with the PS4 selected, which made it uninstallable there.
python3 "$TOOLS/test_panel_console_state.py" || { echo "ABORT: the game panel is describing the wrong console." >&2; exit 1; }

# Following a console that changed address is the only code allowed to REWRITE one, so its
# refusals matter as much as its successes - see the test for the nine cases.
python3 "$TOOLS/test_console_tracker.py" || { echo "ABORT: the console tracker would mis-address a console." >&2; exit 1; }

# THE OWNER'S PLUGIN LIST. /data/GoldHEN/plugins.ini is their file, it survives a reboot and a
# re-jailbreak, and a bad line in it hangs the next game launch with nothing of ours running to undo
# it. This gate lifts the real functions out of server_ps4.c, compiles them on the PC and drives them
# against a temporary file - which is how six defects in them were found at once, including a writer
# that failed every single toggle because rename() here will not replace an existing file.
python3 "$TOOLS/test_gh_ini.py" || { echo "ABORT: the plugins.ini code would damage the owner's plugin list." >&2; exit 1; }

# The 304 lane, and the two copies of etag_matches() that must stay identical.
python3 "$TOOLS/test_etag.py" || { echo "ABORT: the conditional-GET lane is broken, or the two copies have drifted." >&2; exit 1; }

# Embed the current UI. Same generator the PS5 build uses, writing OUR copy of the bundle.
python3 "$HERE/../../ps5-app/onconsole/gen_web_bundle.py" "$WEB" "$HERE/web_bundle.h" \
  || { echo "ABORT: could not regenerate web_bundle.h." >&2; exit 1; }

# Keep our copy of the SQLite reader honest against the PS5 original.
python3 "$TOOLS/ps4_sync_sqmini.py" --check \
  || { echo "ABORT: sqmini.h has drifted from ps5-app/onconsole/server.c - re-run tools/ps4_sync_sqmini.py" >&2; exit 1; }

# The cheat engine this payload now shares with the PS5. Same argument as sqmini above, with more
# at stake: this copy is ~1,400 lines of engine, and a copy nobody checks is a copy that rots.
python3 "$TOOLS/ps4_sync_cheat_core.py" --check \
  || { echo "ABORT: cheat_core.h has drifted from ps5-app/onconsole/server.c - re-run tools/ps4_sync_cheat_core.py" >&2; exit 1; }

# THE DASHBOARD APP THIS ELF CARRIES. tile_bundle.h .incbin's it, so a missing package is a link
# error with no explanation; say so here instead. Build order is the package first, then this - and
# it is one-way on purpose: the package must never carry this ELF back (see tile_bundle.h).
TILE_PKG="$HERE/../tile-pkg/IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg"
if [ "$LITE" = "1" ]; then
  EXTRA="-DPMS_LITE"
  OUT="$HERE/PKG-MUTANT-SHOP-PS4-LITE.elf"
else
  EXTRA=""
  OUT="$HERE/PKG-MUTANT-SHOP-PS4.elf"
  if [ ! -f "$TILE_PKG" ]; then
    echo "ABORT: the PS4 home-screen app is not built - run ps4-app/build-all-wsl.sh." >&2
    exit 1
  fi
fi

# THE TWO VERSIONS MUST NOT AGREE, and that is not a typo. The ELF only installs the package when
# what it carries is NEWER than what the console has, and both numbers are normalised to digits -
# so "01.00" here and "1.00" there are the SAME number and the install silently never happens.
# That shipped once. Never again: this refuses to build when they compare equal or backwards.
if [ "$LITE" = "0" ]; then
  TV=$(grep -m1 "define PS4_TILE_VER" "$HERE/server_ps4.c" | grep -o '"[0-9.]*"' | tr -d '"')
  MV=$(grep -m1 "^VERSION" "$HERE/../tile-pkg/Makefile" | grep -o '[0-9][0-9.]*')
  norm() { echo "$1" | tr -cd '0-9' | sed 's/^0*//;s/^$/0/'; }
  if [ "$(norm "$TV")" != "$(norm "$MV")" ]; then
    echo "ABORT: PS4_TILE_VER ($TV) and the package's VERSION ($MV) must be the same version." >&2
    echo "       They are what the console compares; bump BOTH together." >&2
    exit 1
  fi
  echo "== dashboard app version: $TV (package $MV)"
fi

echo "== syntax check"
"$SDK/bin/orbis-clang" -Wall -DPORT="$PORT" $EXTRA -fsyntax-only -Werror=implicit-function-declaration \
  "$HERE/server_ps4.c" || { echo "ABORT: server_ps4.c does not compile." >&2; exit 1; }

echo "== link"
# -ffile-prefix-map: see the note in the PS5 build. Debug paths only; codegen is unchanged.
"$SDK/bin/orbis-clang" -Wall -g -ffile-prefix-map="$HOME"=/build -DPORT="$PORT" $EXTRA \
  -o "$OUT" "$HERE/server_ps4.c" \
  -lSceAppInstUtil -lSceUserService -lSceSystemService

echo "BUILT: $OUT"
ls -l "$OUT"
file "$OUT"

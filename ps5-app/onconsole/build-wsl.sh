#!/usr/bin/env bash
# Build the on-console HTTP server (serves the shop UI + local API from the PS5 itself).
# Reuses the SDK + user-space clang set up by ps5-app/payload/build-wsl.sh. NO sudo.
# Usage (WSL):  bash build-wsl.sh  [PORT]        (from any directory - it cd's to its own)
set -e
PORT="${1:-8710}"
HERE="$(cd "$(dirname "$0")" && pwd)"
# Every .incbin in payload_bundle.h / tile_bundle.h / cheat_bundle.h is RELATIVE to this
# directory, so a build started from anywhere else failed in the assembler with a path error -
# or worse, found a stale copy somewhere on the way. Work from here, always.
cd "$HERE"
SDK=~/sdk/ps5-payload-sdk
export PS5_PAYLOAD_SDK="$SDK"
export LD_LIBRARY_PATH="$HOME/clang18/usr/lib/llvm-18/lib:$HOME/clang18/usr/lib/x86_64-linux-gnu"

if [ ! -x "$SDK/bin/prospero-clang" ]; then
  echo "SDK not found — run ps5-app/payload/build-wsl.sh once first (it fetches the SDK + clang)." >&2
  exit 1
fi

# python3 is not optional. Without it this script used to skip the version stamp, the UI gate
# and BOTH bundle generators and then link anyway - shipping whatever web_bundle.h and
# cheats.pack happened to be lying around, which is precisely the stale-UI failure the gate
# below exists to stop.
if ! command -v python3 >/dev/null 2>&1; then
  echo "ABORT: python3 is not installed in WSL (sudo apt install python3). The build gates and the bundle generators need it." >&2
  exit 1
fi

# A FAST SYNTAX PASS BEFORE ANY OF THE SLOW WORK. A typo in server.c used to surface only after
# the installer build and both bundle generators - minutes in. -fsyntax-only never reaches the
# assembler, so the .incbin'd blobs do not matter here. An undeclared Sony function is an error
# on clang 18 already; the flag says so in writing, because an inferred signature that compiles
# as an implicit int(...) call is exactly how a guessed syscall crashed the console once.
for src in server.c installer_probe.c; do
  "$SDK/bin/prospero-clang" -Wall -DPORT="$PORT" -fsyntax-only -Werror=implicit-function-declaration \
    "$HERE/$src" "$HERE/jb.c" || { echo "ABORT: $src does not compile - fix it before the bundles are rebuilt." >&2; exit 1; }
done

# THE SPAWNED INSTALLER FIRST. payload_bundle.h .incbin's payloads/pms-installer.elf straight into
# this ELF, so building the shop without rebuilding the installer ships a new shop carrying an old
# installer - and nothing anywhere says so. It was built by hand until 3.31.0, and it drifted.
bash "$HERE/build-installer-wsl.sh"

TOOLS="$HERE/../../tools"

# Keep the UI's version in step with the shipping one: it used to carry a stale hardcoded
# number that flashed on every load before /api/health answered. FATAL now - as a warning it
# scrolled past, and an ELF went out reporting a version the companion did not.
python3 "$TOOLS/stamp_version.py" || { echo "ABORT: could not stamp the version into the UI / server.c / homebrew.js." >&2; exit 1; }

# A page whose script will not parse blanks the app on every device while the server
# keeps answering normally - so refuse to embed one.
python3 "$TOOLS/check_web.py" || { echo "ABORT: the UI script is broken - not building." >&2; exit 1; }

# The two style gates the changelog cites. They were never run by any build until now.
python3 "$TOOLS/i18n_report.py" --check || { echo "ABORT: a language is missing keys the app uses - not building." >&2; exit 1; }
python3 "$TOOLS/message_report.py" --check || { echo "ABORT: a user-facing message is off house style - not building." >&2; exit 1; }

# A PS5 game must not offer a PS4 tab in its mods section. The rule lives in one line of
# modsTabConsoles(), which defers to eligibleConsoles() - so an edit to EITHER can undo it,
# and the symptom only appears on a two-console fleet, for one class of title.
python3 "$TOOLS/test_mods_tabs.py" || { echo "ABORT: the mods console tabs offer the wrong consoles." >&2; exit 1; }

# The PS4 shares THIS file's cheat engine through a generated header. If an edit here moves or
# renames a function the extractor looks for, the PS4 silently stops getting it - so this build
# fails too, not only the PS4 one. The failure belongs where the change was made.
python3 "$TOOLS/ps4_sync_cheat_core.py" --check \
  || { echo "ABORT: the PS4's cheat_core.h no longer matches this file - re-run tools/ps4_sync_cheat_core.py" >&2; exit 1; }

# Following a console that changed address is the only code allowed to REWRITE one, so its
# refusals matter as much as its successes - see the test for the nine cases.
python3 "$TOOLS/test_console_tracker.py" || { echo "ABORT: the console tracker would mis-address a console." >&2; exit 1; }

# The 304 lane, and the two copies of etag_matches() that must stay identical.
python3 "$TOOLS/test_etag.py" || { echo "ABORT: the conditional-GET lane is broken, or the two copies have drifted." >&2; exit 1; }

# Embed the current web/ UI into the ELF (self-extracted to WEB_ROOT on boot) so loading the ELF
# updates the PS5 UI with no separate push. Regenerate the bundle header from web/ first.
python3 "$HERE/gen_web_bundle.py" "$HERE/../../web" "$HERE/web_bundle.h" || { echo "ABORT: could not regenerate web_bundle.h - refusing to ship a console UI that is older than web/index.html." >&2; exit 1; }
# The cheat pack is .incbin'd the same way. A failure here used to be a warning, which meant an
# ELF could ship a stale 32 MB cheats.pack with no failure anywhere. Same rule as the UI.
python3 "$HERE/gen_cheat_bundle.py" "$HERE/../../assets/cheats" "$HERE/cheat_bundle.h" "$HERE/cheats.pack" || { echo "ABORT: could not regenerate the cheat bundle - refusing to ship a stale cheats.pack." >&2; exit 1; }

# Link set mirrors the known-working reference implementation: the installer appears to need the
# wider system-library context, not AppInstUtil alone. -lSceRegMgr is gone: grep finds no RegMgr
# symbol in server.c or jb.c, and the install drive is the console's own setting with no registry
# key behind it (see memory: install drive is not selectable). -g, never -O2 - see the installer.
# -ffile-prefix-map: the DWARF that -g emits records the ABSOLUTE path of every header the
# SDK pulled in, which put the build account's home directory inside a 45 MB binary published
# on a release page. This rewrites recorded paths only - it does not touch codegen, so the
# rule above (-g, never -O2, because -O2 breaks installs) still holds.
"$SDK/bin/prospero-clang" -Wall -g -ffile-prefix-map="$HOME"=/build -DPORT="$PORT" \
  -o "$HERE/PKG-MUTANT-SHOP.elf" "$HERE/server.c" "$HERE/jb.c" \
  -lkernel_sys -lSceNotification -lSceUserService -lSceSystemService \
  -lSceAppInstUtil -lScePad -lSceSsl -lSceHttp
echo "BUILT: $HERE/PKG-MUTANT-SHOP.elf"
file "$HERE/PKG-MUTANT-SHOP.elf"

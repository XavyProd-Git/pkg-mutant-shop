#!/usr/bin/env bash
# Build pms-installer.elf — the process Payload Manager spawns for each install.
#
# It was built by hand until now, which is why it drifted out of step with server.c more than once:
# the main ELF embeds payloads/pms-installer.elf with .incbin, so forgetting this step ships a NEW
# shop with an OLD installer and no warning anywhere. build-wsl.sh calls this first now.
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
SDK="$HOME/sdk/ps5-payload-sdk"
export PS5_PAYLOAD_SDK="$SDK"
export LD_LIBRARY_PATH="$HOME/clang18/usr/lib/llvm-18/lib:$HOME/clang18/usr/lib/x86_64-linux-gnu"

if [ ! -x "$SDK/bin/prospero-clang" ]; then
  echo "SDK not found — run ps5-app/payload/build-wsl.sh once first." >&2
  exit 1
fi

# -g, NOT -O2: these are the flags that built the binary proven on hardware. The installer is a few
# hundred lines of straight-line code, so there is nothing to gain from optimising it and a
# provable regression to lose if the flags ever turn out to matter.
# -ffile-prefix-map keeps the build account's home out of the DWARF - see the note in
# build-wsl.sh. Paths only; -g stays, and -O2 still must not come back (it breaks installs).
"$SDK/bin/prospero-clang" -Wall -g -ffile-prefix-map="$HOME"=/build \
  -o "$HERE/payloads/pms-installer.elf" \
  "$HERE/installer_probe.c" "$HERE/jb.c" \
  -lkernel_sys -lSceNotification -lSceUserService -lSceSystemService -lSceAppInstUtil
echo "BUILT: $HERE/payloads/pms-installer.elf"
ls -l "$HERE/payloads/pms-installer.elf"

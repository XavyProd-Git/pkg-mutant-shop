#!/usr/bin/env bash
# Reproducible WSL build of pkg-mutant-shop.elf — NO SUDO REQUIRED.
# Usage (from WSL Ubuntu):  bash build-wsl.sh  [PMS_URL]
#   PMS_URL = your companion URL the tile opens (default http://10.0.0.76:8710)
set -e
PMS_URL="${1:-http://10.0.0.76:8710}"
HERE="$(cd "$(dirname "$0")" && pwd)"
cd ~

# 1) ps5-payload-dev SDK (one-time)
if [ ! -d ~/sdk/ps5-payload-sdk ]; then
  echo "[setup] fetching ps5-payload SDK..."
  curl -sL -o ps5sdk.zip https://github.com/ps5-payload-dev/sdk/releases/latest/download/ps5-payload-sdk.zip
  rm -rf ~/sdk && python3 -m zipfile -e ps5sdk.zip ~/sdk/
  chmod +x ~/sdk/ps5-payload-sdk/bin/* || true
fi

# 2) clang-18 into user space (one-time, NO sudo — just downloads .debs and extracts them)
if [ ! -x ~/clang18/usr/lib/llvm-18/bin/clang ]; then
  echo "[setup] fetching clang-18 to ~/clang18 (no sudo)..."
  mkdir -p ~/clangdl && cd ~/clangdl
  for p in clang-18 lld-18 libclang-cpp18 libllvm18 libc6 libgcc-s1 libstdc++6 libz3-4 libedit2 libtinfo6 libncurses6; do
    apt-get download "$p" 2>/dev/null || true
  done
  for deb in *.deb; do dpkg-deb -x "$deb" ~/clang18; done
  cd ~
fi

SDK=~/sdk/ps5-payload-sdk
BIN="$HOME/clang18/usr/lib/llvm-18/bin"
# the SDK's clang wrapper (as a plain copy) looks for llvm-<tool>; provide them:
for t in clang clang++ ar nm objcopy ranlib strip; do ln -sf "$t" "$BIN/llvm-$t"; done
ln -sf ld.lld "$BIN/llvm-lld" 2>/dev/null || true
# point the SDK's llvm-config at our user-space clang:
printf '#!/bin/bash\ncase "$1" in\n  --bindir) echo "%s";;\n  --libdir) echo "%s/../lib";;\n  *) echo "";;\nesac\n' "$BIN" "$BIN" > "$SDK/bin/prospero-llvm-config"
chmod +x "$SDK/bin/prospero-llvm-config"

# 3) build
export PS5_PAYLOAD_SDK="$SDK"
export LD_LIBRARY_PATH="$HOME/clang18/usr/lib/llvm-18/lib:$HOME/clang18/usr/lib/x86_64-linux-gnu"
# NOTE: link -lkernel_sys for jb.c (escalation) + the runtime module loader. Do NOT link
# -lSceAppInstUtil — it makes the elf fail to load; we LoadStartModule + resolve it at runtime.
"$SDK/bin/prospero-clang" -Wall -g -DPMS_URL="\"$PMS_URL\"" \
  -o "$HERE/pkg-mutant-shop.elf" "$HERE/main.c" "$HERE/jb.c" \
  -lkernel_sys -lSceNotification -lSceSystemService -lSceUserService
echo "BUILT: $HERE/pkg-mutant-shop.elf"
file "$HERE/pkg-mutant-shop.elf"

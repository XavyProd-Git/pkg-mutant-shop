#!/usr/bin/env bash
# Build the PS4 dashboard app package for PKG MUTANT SHOP.
#
# Produces IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg - a real, fake-signed PS4 application package
# that puts the shop on the console's home screen. Installing it goes through the SAME lane as any
# other game (our own BGFT install), because it IS an ordinary PS4 package: content type 0x1A,
# category 'gd', and our own companion/pkg_meta.py reads it like it reads a retail title.
#
# One-time setup this performs by itself:
#   * the OpenOrbis PS4 Toolchain (clang target, create-fself, create-gp4, PkgTool.Core)
#   * libssl 1.1, unpacked into the toolchain directory rather than installed - PkgTool's bundled
#     .NET runtime needs it and this box has libssl 3 and no root. Extracting the .deb locally and
#     pointing LD_LIBRARY_PATH at it keeps the whole build inside one script and touches nothing.
#
# Usage (WSL):  bash build-wsl.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"
cd "$HERE"

TOOLROOT="$HOME/ps4tool"
export OO_PS4_TOOLCHAIN="$TOOLROOT/OpenOrbis/OpenOrbis/PS4Toolchain"
export PATH="$HOME/clang18/usr/lib/llvm-18/bin:$PATH"
export DOTNET_SYSTEM_GLOBALIZATION_INVARIANT=1

TC_URL="https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain/releases/download/v0.5.4/toolchain-llvm-18.tar.gz"
SSL_URL="http://security.ubuntu.com/ubuntu/pool/main/o/openssl/libssl1.1_1.1.1f-1ubuntu2.24_amd64.deb"

if [ ! -x "$OO_PS4_TOOLCHAIN/bin/linux/create-fself" ]; then
  echo "== fetching the OpenOrbis PS4 toolchain (one time, ~150 MB)"
  mkdir -p "$TOOLROOT/OpenOrbis"
  curl -L -o "$TOOLROOT/toolchain.tar.gz" "$TC_URL"
  tar xzf "$TOOLROOT/toolchain.tar.gz" -C "$TOOLROOT/OpenOrbis"
  chmod +x "$OO_PS4_TOOLCHAIN"/bin/linux/* || true
fi

# PkgTool's runtime wants libssl 1.1 and Ubuntu 24.04 ships 3. No root here, so unpack it beside
# the toolchain and load it from there - nothing outside this directory is touched.
SSLDIR="$TOOLROOT/ssl11"
if [ ! -f "$SSLDIR/usr/lib/x86_64-linux-gnu/libssl.so.1.1" ]; then
  echo "== unpacking libssl 1.1 for PkgTool (one time, local only)"
  mkdir -p "$SSLDIR" && cd "$SSLDIR"
  curl -L -o libssl.deb "$SSL_URL"
  dpkg-deb -x libssl.deb . 2>/dev/null || { ar x libssl.deb && tar xf data.tar.*; }
  cd "$HERE"
fi
export LD_LIBRARY_PATH="$SSLDIR/usr/lib/x86_64-linux-gnu:$LD_LIBRARY_PATH"

# The three files every PS4 application package carries besides ours. They come from the toolchain
# rather than being checked in: they are Sony-provided stubs and this way they stay in step with it.
mkdir -p sce_sys/about sce_module
for f in sce_sys/about/right.sprx sce_module/libSceFios2.prx sce_module/libc.prx; do
  [ -f "$f" ] || cp "$OO_PS4_TOOLCHAIN/samples/hello_world/$f" "$f"
done

# THE PAYLOAD THIS PACKAGE CARRIES is the LITE one - the same shop with no package inside it. That
# is what lets the icon start the shop without creating a cycle: this package carries lite, and the
# full payload carries this package. Build order is lite -> package -> full, which is what
# ps4-app/build-all-wsl.sh does. Handing this the FULL payload would put a package inside a package.
PAYLOAD="$HERE/../onconsole/PKG-MUTANT-SHOP-PS4-LITE.elf"
if [ ! -f "$PAYLOAD" ]; then
  echo "ABORT: the lite payload is not built - run ps4-app/build-all-wsl.sh." >&2
  exit 1
fi
cp "$PAYLOAD" pms-payload.elf

if [ ! -f sce_sys/icon0.png ]; then
  echo "ABORT: sce_sys/icon0.png is missing - the console needs an icon for the tile." >&2
  exit 1
fi

echo "== building"
make clean >/dev/null 2>&1 || true
make

PKG="IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg"
if [ ! -f "$PKG" ]; then
  echo "ABORT: no package was produced." >&2
  exit 1
fi
# PROVE THE PAYLOAD IS ACTUALLY IN THERE. The package is a fixed 6,619,136 bytes whether it holds
# the payload or not - the filesystem pads - so size tells you nothing, and an app whose package
# lost its payload cannot start the shop however well its code is written. Extract the finished
# package and look.
echo "== checking the package really carries the payload"
CHK=$(mktemp -d)
"$OO_PS4_TOOLCHAIN/bin/linux/PkgTool.Core" pkg_extract "$PKG" "$CHK" >/dev/null 2>&1 || true
GOT="$CHK/uroot/pms-payload.elf"
WANT=$(stat -c%s pms-payload.elf)
if [ ! -f "$GOT" ]; then
  echo "ABORT: the built package does not contain pms-payload.elf." >&2
  rm -rf "$CHK"; exit 1
fi
HAVE=$(stat -c%s "$GOT")
if [ "$HAVE" != "$WANT" ]; then
  echo "ABORT: the payload inside the package is $HAVE bytes, expected $WANT." >&2
  rm -rf "$CHK"; exit 1
fi
if [ "$(sha256sum "$GOT" | cut -d" " -f1)" != "$(sha256sum pms-payload.elf | cut -d" " -f1)" ]; then
  echo "ABORT: the payload inside the package is not the one we put in." >&2
  rm -rf "$CHK"; exit 1
fi
echo "   payload inside: $HAVE bytes, sha256 matches"
rm -rf "$CHK"

echo "BUILT: $HERE/$PKG"
ls -l "$PKG"

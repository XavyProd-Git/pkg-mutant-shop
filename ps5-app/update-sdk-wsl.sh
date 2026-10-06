#!/usr/bin/env bash
# Refresh the ps5-payload-dev SDK, because the console's firmware moves and ours does not.
#
# WHY THIS EXISTS. build-wsl.sh fetches the SDK exactly once - `if [ ! -d ~/sdk/ps5-payload-sdk ]` -
# so the toolchain stays pinned to whatever was current the day the machine was set up, and nothing
# ever says otherwise. The SDK is where the KERNEL OFFSETS PER FIRMWARE live: crt1.o carries a
# switch on kernel_get_fw_version() and returns -ENOSYS for a firmware it has never heard of, so our
# ELF cannot establish kernel read/write and dies before it reaches its own first log line.
#
# Measured 2026-10-01 on the owner's PS5: the console was updated past 13.40, the installed SDK knew
# up to 13.40, and from then on Payload Manager accepted all 45 MB of the shop and started nothing -
# no process, no boot line, no notification, four times running. The payloads that kept working were
# third-party builds that had been rebuilt for the new firmware; an old third-party one (ps5debug-NG
# v1.3.2) failed exactly like ours, which is worth remembering because it makes "is it our code or
# the console" look answered when it is not. Nothing about our own source was wrong.
#
# Usage (WSL):  bash ps5-app/update-sdk-wsl.sh [--check]
#   --check   say which firmwares the installed SDK knows and what upstream has; change nothing.
set -e

SDK="$HOME/sdk/ps5-payload-sdk"
URL="https://github.com/ps5-payload-dev/sdk/releases/latest/download/ps5-payload-sdk.zip"
KERNEL_C="https://raw.githubusercontent.com/ps5-payload-dev/sdk/master/crt/kernel.c"
BIN="$HOME/clang18/usr/lib/llvm-18/bin"

# Firmware is BCD: 0x13400000 -> 13.40. Decode every immediate in crt1.o that has that shape.
raw_candidates() {
  local obj="$1" dis=""
  for cand in "$BIN/llvm-objdump" "$(command -v llvm-objdump || true)" "$(command -v objdump || true)"; do
    if [ -n "$cand" ] && [ -x "$cand" ]; then dis="$cand"; break; fi
  done
  [ -n "$dis" ] || return 0
  "$dis" -d "$obj" 2>/dev/null \
    | grep -oiE '\$0x[0-9a-f]{7,8}' | tr -d '$' | sort -u \
    | while read -r v; do
        n=$((v))
        if [ $((n & 0xFFFF)) -eq 0 ]; then
          printf '%x.%02x\n' $(( (n >> 24) & 0xFF )) $(( (n >> 16) & 0xFF ))
        fi
      done | sort -u
}

# ONLY WHAT UPSTREAM HAS A `case` FOR IS A FIRMWARE. crt1.o holds plenty of other constants that
# decode as valid BCD - 0x13370000 ("1337") sits in the middle of the real ones, and 30.00, 40.00
# and 48.00 decode cleanly too - so the installed list is intersected with upstream's rather than
# guessed at by shape. Reading the shape alone reported the ceiling as "48.00", which is nonsense.
upstream_list() {
  curl -sL --max-time 25 "$KERNEL_C" 2>/dev/null \
    | grep -oiE 'case +0x[0-9a-f]{7,8}:' | grep -oiE '0x[0-9a-f]{7,8}' | sort -u \
    | while read -r v; do
        n=$((v))
        if [ $((n & 0xFFFF)) -eq 0 ]; then
          printf '%x.%02x\n' $(( (n >> 24) & 0xFF )) $(( (n >> 16) & 0xFF ))
        fi
      done | sort -u
}

vsort() { sort -t. -k1,1n -k2,2n; }

UP="$(upstream_list || true)"

known() {
  if [ -z "$UP" ]; then
    raw_candidates "$1" | vsort          # offline: show candidates rather than nothing
  else
    comm -12 <(raw_candidates "$1") <(echo "$UP" | sort -u) | vsort
  fi
}

report() {
  local obj="$1"
  echo "   $(known "$obj" | tr '\n' ' ' | fold -s -w 96 | sed '2,$s/^/   /')"
  echo "   highest: $(known "$obj" | tail -1)"
  if [ -n "$UP" ]; then
    local missing
    missing="$(comm -13 <(known "$obj") <(echo "$UP" | vsort) | tr '\n' ' ')"
    if [ -n "${missing// /}" ]; then
      echo "   MISSING vs upstream: $missing"
    else
      echo "   nothing missing - this SDK matches upstream"
    fi
  fi
}

if [ -d "$SDK" ]; then
  echo "installed SDK knows these firmwares:"
  report "$SDK/target/lib/crt1.o"
else
  echo "no SDK installed at $SDK"
fi
[ -n "$UP" ] && echo "upstream's highest: $(echo "$UP" | vsort | tail -1)"

if [ "$1" = "--check" ]; then
  exit 0
fi

echo
echo "[update] fetching the latest SDK..."
cd ~
curl -sL -o ps5sdk-new.zip "$URL"
[ -s ps5sdk-new.zip ] || { echo "ABORT: the download is empty"; exit 1; }

STAMP="$(date +%Y%m%d-%H%M%S)"
if [ -d "$SDK" ]; then
  echo "[update] keeping the current SDK as $SDK.bak-$STAMP"
  mv "$SDK" "$SDK.bak-$STAMP"
fi
mkdir -p ~/sdk
python3 -m zipfile -e ps5sdk-new.zip ~/sdk/
rm -f ps5sdk-new.zip
chmod +x "$SDK"/bin/* 2>/dev/null || true

# THE CLANG SHIM LIVES INSIDE THE SDK DIRECTORY, so extracting a new one destroys it. build-wsl.sh
# writes it on first setup only, so a plain re-extract leaves a toolchain that cannot find clang -
# and that surfaces much later as an unrelated linker error.
for t in clang clang++ ar nm objcopy ranlib strip; do ln -sf "$t" "$BIN/llvm-$t" 2>/dev/null || true; done
ln -sf ld.lld "$BIN/llvm-lld" 2>/dev/null || true
printf '#!/bin/bash\ncase "$1" in\n  --bindir) echo "%s";;\n  --libdir) echo "%s/../lib";;\n  *) echo "";;\nesac\n' "$BIN" "$BIN" > "$SDK/bin/prospero-llvm-config"
chmod +x "$SDK/bin/prospero-llvm-config"

echo
echo "[update] the SDK now knows:"
report "$SDK/target/lib/crt1.o"
echo
echo "Now rebuild:  bash ps5-app/onconsole/build-wsl.sh"
echo "The previous SDK is at $SDK.bak-$STAMP if anything has to go back."

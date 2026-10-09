#!/usr/bin/env bash
# Build the PKG MUTANT SHOP dashboard tile (icon + eboot that opens our app + installer payload).
# Reuses the SDK + user-space clang set up by ps5-app/payload/build-wsl.sh. NO sudo.
# Usage (WSL):  bash build-wsl.sh  [PMS_URL]
set -e
PMS_URL="${1:-http://10.0.0.76:8710}"
REPORT_HOST="${2:-10.0.0.76}"      # companion PC that runs the PS5-log listener (:9097)
TITLE_ID="PKGM00001"
HERE="$(cd "$(dirname "$0")" && pwd)"
SDK=~/sdk/ps5-payload-sdk
SAMP="$SDK/samples/install_app"
export PS5_PAYLOAD_SDK="$SDK"
export LD_LIBRARY_PATH="$HOME/clang18/usr/lib/llvm-18/lib:$HOME/clang18/usr/lib/x86_64-linux-gnu"

WORK=~/pms-tile
rm -rf "$WORK" && mkdir -p "$WORK/$TITLE_ID/sce_sys"
cp "$SAMP/make_fself.py" "$WORK/"
cp "$HERE/tile-install.c" "$WORK/"          # OUR self-diagnosing installer (not the silent sample)
cp "$HERE/eboot.c" "$WORK/"
cp "$HERE/../../web/assets/icon0.png" "$WORK/$TITLE_ID/icon0.png"   # OUR logo as the tile icon

cat > "$WORK/$TITLE_ID/sce_sys/param.json" <<EOF
{
    "applicationCategoryType": 0,
    "localizedParameters": { "defaultLanguage": "en-US", "en-US": { "titleName": "PKG MUTANT SHOP" } },
    "titleId": "$TITLE_ID"
}
EOF
cp "$WORK/$TITLE_ID/sce_sys/param.json" "$WORK/$TITLE_ID/sce_sys/param.json.system"

cd "$WORK"
AUTHID=0x3800000000000022
AUTHINFO="00 00 00 00 00 00 00 00 00 00 00 00 00 1C 00 40 00 FF 00 00 00 00 00 80 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 80 00 40 00 40 00 00 00 00 00 00 00 80 00 00 00 00 00 00 00 08 00 40 FF FF 00 00 00 F0 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00 00"
SDKVER=0x07590001

echo "[1/3] installer payload (NEEDED AppInstUtil, no Ipmi - mirrors dump_installer; reports to $REPORT_HOST:9097)"
"$SDK/bin/prospero-clang" -Wall -g \
  -DTITLE_ID="\"$TITLE_ID\"" -DREPORT_HOST="\"$REPORT_HOST\"" \
  -lSceNotification -lSceAppInstUtil -lSceUserService -lSceSystemService -lSceFsInternalForVsh \
  -o payload.elf tile-install.c
echo "[2/3] eboot (opens $PMS_URL)"
"$SDK/bin/prospero-clang" -Wall -g -DPMS_URL="\"$PMS_URL\"" \
  -lSceSystemService -lSceUserService -o eboot.elf eboot.c
# prospero-clang forces PIE (ET_DYN); make_fself needs ET_SCE_DYNAMIC (0xFE18) — the real PS5 app type
printf '\x18\xfe' | dd of=eboot.elf bs=1 seek=16 count=2 conv=notrunc 2>/dev/null
echo "[3/3] fake-sign eboot"
./make_fself.py --ptype fake --paid $AUTHID --auth-info "$AUTHINFO" \
  --app-version $SDKVER --fw-version $SDKVER eboot.elf "$TITLE_ID/eboot.bin"

# copy results into the project
mkdir -p "$HERE/$TITLE_ID/sce_sys"
cp payload.elf                          "$HERE/install-tile-payload.elf"
cp "$TITLE_ID/eboot.bin"                "$HERE/$TITLE_ID/eboot.bin"
cp "$TITLE_ID/sce_sys/param.json"        "$HERE/$TITLE_ID/sce_sys/param.json"
cp "$TITLE_ID/sce_sys/param.json.system" "$HERE/$TITLE_ID/sce_sys/param.json.system"
cp "$TITLE_ID/icon0.png"                 "$HERE/$TITLE_ID/icon0.png"
echo "=== BUILT ==="
ls -la "$HERE/install-tile-payload.elf" "$HERE/$TITLE_ID/eboot.bin"
file "$HERE/$TITLE_ID/eboot.bin"

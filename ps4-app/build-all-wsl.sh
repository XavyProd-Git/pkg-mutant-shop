#!/usr/bin/env bash
# Build the whole PS4 side, in the one order that works.
#
# There are three artefacts and they depend on each other in a line, not a circle:
#
#   0. pms-agent.prx                  the in-game agent, embedded by BOTH payloads
#   1. PKG-MUTANT-SHOP-PS4-LITE.elf   the shop, with no package inside it
#   2. IV0000-PKGM00001_00-...pkg     the home-screen app, carrying (1)
#   3. PKG-MUTANT-SHOP-PS4.elf        the shop, carrying (2)
#
# WHY IT IS SHAPED LIKE THAT. The user asked for two things that pull against each other: the ELF
# must carry the home-screen app and install it (as the PS5 ELF does with its tile), AND pressing
# the icon must start the shop by itself with every PC switched off. The first means ELF contains
# package; the second means package contains ELF. Both at once is a package inside a package inside
# a package, growing with every rebuild.
#
# The lite build cuts it. Lite is the same shop - same UI, same ports, same install engine - minus
# the one thing it cannot have: a copy of the package. So the icon carries a payload that can serve
# everything, and the full payload carries the icon. Nothing contains itself, and the sizes are
# fixed: ~1.7 MB, ~6.6 MB, ~8.3 MB.
#
# The only thing lite gives up is installing the home-screen app - and whoever is running lite got
# there by pressing that app, so it is already installed.
#
# Usage (WSL):  bash ps4-app/build-all-wsl.sh
set -e
HERE="$(cd "$(dirname "$0")" && pwd)"

echo "======== 0/4  the in-game agent plugin (both payloads embed it)"
# FIRST, because both payloads .incbin the .prx and an incbin of a stale file is a silent
# regression: the shop would lay down last build's agent and report success.
bash "$HERE/plugin/build-wsl.sh"

echo
echo "======== 1/4  the lite payload (no package inside)"
bash "$HERE/onconsole/build-wsl.sh" --lite

echo
echo "======== 2/4  the home-screen app package (carries the lite payload)"
bash "$HERE/tile-pkg/build-wsl.sh"

echo
echo "======== 3/4  the full payload (carries the package)"
bash "$HERE/onconsole/build-wsl.sh"

echo
echo "======== done"
ls -l "$HERE/onconsole/PKG-MUTANT-SHOP-PS4-LITE.elf" \
      "$HERE/tile-pkg/IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg" \
      "$HERE/onconsole/PKG-MUTANT-SHOP-PS4.elf"

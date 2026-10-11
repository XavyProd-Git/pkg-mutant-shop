/* The dashboard tile package, linked straight into the ELF.
 *
 * Same .incbin trick as payload_bundle.h: the assembler drops the raw bytes into .rodata, so
 * there is no multi-megabyte C array to compile and one ELF carries everything it needs.
 *
 * Why a PKG and not a database edit: on FW 12.70 `sceAppInstUtilAppInstallTitleDir` is not
 * exported at all (probed live: resolves to 0x0 by name AND by NID), and calling
 * `sceAppInstUtilAppInstallAll` from our own process returns 0 and does nothing — ShadowMount
 * only gets it to work by patching SceShellCore to make the call from inside ShellCore.
 * `sceAppInstUtilAppInstallPkg` IS exported (0x80035ea60) and hands the job to ShellCore's own
 * installer queue, which is the same path a store download takes: it writes /user/appmeta/<TID>
 * and the database rows itself, so the tile appears live with no reboot and a re-run updates it.
 * That is what Elf Arsenal and CheatRunner both do, and what this project did before.
 *
 * Writing app.db by hand instead is what our own ps5-app/tile-pkg/README.md warned about: the
 * console rewrites the launch target to psgm:play, finds no game, and the entry is garbage
 * collected — taking the dashboard's database consistency with it.
 *
 * Built by ps5-app/tile-pkg/build (LibProsperoPkg, in-process, no Sony tooling).
 */
#ifndef TILE_BUNDLE_H
#define TILE_BUNDLE_H

#define TB_INCBIN(sym, file)                    \
  __asm__(".section .rodata\n"                  \
          ".global " #sym "\n"                  \
          ".balign 16\n"                        \
          #sym ":\n"                            \
          ".incbin \"" file "\"\n"              \
          ".global " #sym "_end\n"              \
          #sym "_end:\n"                        \
          ".previous\n");                       \
  extern const unsigned char sym[];             \
  extern const unsigned char sym##_end[];

TB_INCBIN(tb_tile_pkg, "tile/pms-tile.pkg")

#endif /* TILE_BUNDLE_H */

/* The PS4 dashboard app package, linked straight into this ELF.
 *
 * Same .incbin trick the PS5 build uses for its tile: the assembler drops the raw bytes into
 * .rodata, so there is no multi-megabyte C array to compile and one ELF carries everything it
 * needs to put the shop on the console's home screen.
 *
 * ONE DIRECTION ONLY. This ELF carries the package; the package must never carry this ELF. It did
 * once - the app shipped the payload so it could start the shop itself - and that is a cycle: the
 * ELF would contain a package containing the ELF, and every rebuild would embed the previous one.
 * ps4-app/tile-pkg/build-wsl.sh says the same thing at the other end of it. Build order is the
 * package first, then this.
 *
 * Why a package and not a database edit: writing app.db by hand is what the PS5 side warns about
 * in ps5-app/tile-pkg/README.md - the console rewrites the launch target, finds no game, and takes
 * the dashboard's database consistency with it. A real package handed to the console's own
 * installer is the only thing that writes those rows correctly, and on the PS4 that is our ordinary
 * BGFT lane, which needs no special privilege at all.
 *
 * Built by ps4-app/tile-pkg/build-wsl.sh with the OpenOrbis PS4 toolchain.
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

TB_INCBIN(tb_ps4_tile_pkg, "../tile-pkg/IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg")

#endif /* TILE_BUNDLE_H */

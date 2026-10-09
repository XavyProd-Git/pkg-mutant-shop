/* The in-game agent plugin, linked straight into this ELF.
 *
 * WHY IT TRAVELS INSIDE THE PAYLOAD. The agent (ps4-app/plugin/) is what makes cheats work on a
 * PS4 at all: this payload cannot reach a running game's memory from outside - mdbg and ptrace are
 * both EPERM from here, measured on 13.52 - so a small plugin does the reading and writing from
 * inside the game, and GoldHEN's plugin loader is what puts it there. That only works if the .prx
 * is actually on the console, and asking an owner to copy a file over FTP before cheats work is a
 * step that will be missed, blamed on the app, and impossible to diagnose from a screenshot.
 *
 * So the shop carries it, the same way it carries the home-screen package and the web UI. 54 KB
 * against an 8.9 MB ELF; the cheat LIBRARY is a different matter and stays on disk, synced from a
 * PC, because that is 27 MB and this payload is injected into a shared system daemon.
 *
 * NO CYCLE HERE, unlike tile_bundle.h. The plugin does not contain the shop and never will - it is
 * deliberately a pair of hands with no knowledge of cheats, files or titles - so this direction is
 * the only one there is and no build-order trap comes with it.
 *
 * Built by ps4-app/plugin/build-wsl.sh with the OpenOrbis toolchain (no GoldHEN SDK needed).
 */
#ifndef AGENT_BUNDLE_H
#define AGENT_BUNDLE_H

#define AB_INCBIN(sym, file)                    \
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

AB_INCBIN(ab_pms_agent_prx, "../plugin/build/pms-agent.prx")

#endif /* AGENT_BUNDLE_H */

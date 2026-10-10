/* The PS4's helper payloads, embedded with .incbin the same way the home-screen package and the
   in-game agent are. 266 KB against an 8.7 MB ELF.

   WHY THE PS4 CARRIES ITS OWN AND NOT THE PS5's. These are different binaries for a different
   console - ftpsrv-ps4.elf is not ftpsrv-ps5.elf - and the catalogue keeps them apart by the folder
   the owner put them in, not by parsing a filename for the letters "ps4".

   THE CHEAT LIBRARY IS THE PRECEDENT FOR WHAT IS *NOT* HERE. agent_bundle.h already records the
   rule: "54 KB against an 8.9 MB ELF; the cheat LIBRARY is a different matter and stays on disk,
   synced from a PC, because that is 27 MB and this payload is injected into a shared system
   daemon." The homebrew packages are 87 MB for this console and follow the same rule - they are
   seeded into HB_DIR once and installed from there, never embedded. */
#ifndef PAYLOAD_BUNDLE_PS4_H
#define PAYLOAD_BUNDLE_PS4_H

#define P4PB_INCBIN(sym, file)                  \
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

P4PB_INCBIN(p4pb_ftpsrv,  "payloads/ftpsrv.elf")
P4PB_INCBIN(p4pb_nanodns, "payloads/nanodns.elf")

/* `port` proves it is running; `autostart` is permission to start it at boot. Two fields, because
   one field cannot answer both questions - see the long note in the PS5's payload_bundle.h, where
   a payload that runs without binding anything was permanently mis-reported.

   NOTHING AUTO-STARTS ON THIS CONSOLE. The PS5 can ask Payload Manager to run something; the PS4's
   only loader is GoldHEN's, and the one way to reach it is to POST an ELF at :9090 - which is also
   the only way to find out whether it is listening, because a bare connect to that port stops it
   listening. Starting payloads unasked, over a lane that cannot be probed first, is not a thing to
   do behind the owner's back on every boot. They ship, and the panel sends them when asked. */
typedef struct { const char *name; const char *filename; int port; int autostart; int udp;
                 const unsigned char *data; const unsigned char *end; } p4pb_entry_t;

static const p4pb_entry_t PS4_PAYLOAD_BUNDLE[] = {
  { "ftpsrv",  "ftpsrv.elf",  2121, 0, 0, p4pb_ftpsrv,  p4pb_ftpsrv_end  },
    /* 53/UDP. The owner ran this from the panel and it worked - and the panel still said
     "no way to tell", because nothing answers a TCP connect on 53 and nanodns replies to no
     query sent from the LAN. `udp` makes the console test it by trying to take the port. */
  { "nanodns", "nanodns.elf",   53, 0, 1, p4pb_nanodns, p4pb_nanodns_end },
};
#define PS4_PAYLOAD_BUNDLE_COUNT 2

#endif

/* Helper payloads embedded via .incbin — the assembler links the raw bytes straight into the
   ELF, so there is no multi-megabyte C array to compile (a hex-array header for these two was
   33 MB of source). One ELF therefore ships the whole service. */
#ifndef PAYLOAD_BUNDLE_H
#define PAYLOAD_BUNDLE_H

#define PB_INCBIN(sym, file)                    \
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

PB_INCBIN(pb_shadowmount, "payloads/shadowmountplus.elf")
/* etaHEN IS NO LONGER BUNDLED (v3.33.0). We never started it - it was carried "so the user always
   has the build we tested against". They do not want it, we do not call it, and nothing in our lane
   reads it: pb_etahen appeared only in the table below, and the companion's host_payload_path()
   resolves through Payload Manager's own directory, not our copy, and only in the legacy "v2" mode.
   Carrying it cost 4.7 MB in this ELF and 4.7 MB written to the console on every boot, and its
   presence on disk was the only thing that made starting it by accident possible.
   The file is still in ps5-app/onconsole/payloads/ for reference; it is simply not embedded. */
/* OUR base-game installer. It exists because sceAppInstUtilInstallByPackage returns
   0x80B2116F from a payload INJECTED into a hijacked host (which the shop ELF is) and
   succeeds from a freshly SPAWNED process. Shipped, never auto-started - Payload Manager
   spawns it per install. See /api/engine/install-spawn. */
PB_INCBIN(pb_installer,   "payloads/pms-installer.elf")

/* THE OWNER'S OWN SET (3.84.0). These are the payloads they keep in
   "C:/Mutant Payloads & HomeBrews/Payloads/PS5", refreshed into ps5-app/onconsole/payloads by
   tools/sync_payload_bins.py so a build never depends on a folder that exists on one PC. They are
   here so the Payloads & Homebrews panel works on a console with every PC switched off: the shop
   writes them to PB_DIR at boot and can hand any of them to Payload Manager on request.
   12.6 MB of ELF buys a console that needs nothing from anywhere to re-arm itself. */
PB_INCBIN(pb_ftpsrv,      "payloads/ftpsrv.elf")
PB_INCBIN(pb_nanodns,     "payloads/nanodns.elf")
PB_INCBIN(pb_kstuff,      "payloads/kstuff.elf")
PB_INCBIN(pb_onionhen,    "payloads/onionhen.elf")
PB_INCBIN(pb_pldmgr,      "payloads/pldmgr.elf")
PB_INCBIN(pb_wkauto,      "payloads/webkit-autoloader-installer.elf")

/* `port` is the service the payload provides, and `autostart` is whether we may start it at boot.
   THESE USED TO BE ONE FIELD and that was a mistake worth writing down, because the console proved
   it: `port > 0` meant both "this is how you tell it is running" and "launch it when that port is
   free", so a payload that runs WITHOUT binding anything could never be described honestly. Payload
   Manager lists shadowmountplus.elf at a live pid while :10101 is closed - measured 2026-09-30 - so
   under the old rule it looked stopped AND was a candidate to be started again on every single
   boot. Two questions, two fields.

     port       the TCP port that PROVES it is running, or 0 when nothing listens for it.
     autostart  1 only for a payload that provides a service, binds its port reliably, and touches
                nothing underneath it. Everything else ships and waits to be asked.

   Neither Elf Arsenal nor etaHEN is bundled, and neither is the install host. Installs go through
   OUR spawned installer (pms-installer, below), which needs nothing to be running beforehand
   except Payload Manager. The history, kept because it explains the shape of this table:
   Arsenal and etaHEN both served DPI v2 on :12800 and could not coexist - whoever bound first
   kept it and the other retried forever, spewing "bind | Address already in use" notifications
   and a 2 MB log; Arsenal also dragged in nanodns, garlic and ftpsrv. etaHEN was then carried for
   a while at port 0, and briefly at 12800, which was a mistake: it patches the kernel/ShellCore,
   this console's own boot chain (pldmgr -> kstuff_lite -> shadowmountplus, per the user's
   autoload.txt) deliberately does not run it, and auto-starting it put a second kernel-touching
   daemon next to kstuff_lite that had never been part of a working boot - the first fake-signed
   PS4 title launched afterwards panicked the console. A payload that changes the jailbreak layer
   is the user's call to make, never a side effect of installing our app. Since 3.33.0 it is not
   embedded at all (see above), so there is no etaHEN entry in the table and nothing of ours can
   start it by accident. server.c's REST_STOP still names it, only to stand down a copy the user
   runs before rest mode. */
typedef struct { const char *name; const char *filename; int port; int autostart; int udp;
                 const unsigned char *data; const unsigned char *end; } pb_entry_t;

static const pb_entry_t PAYLOAD_BUNDLE[] = {
  /* 10101, NOT 9021. ShadowMount announces its own listener in its log:
       [API] HTTP/JSON ready: http://127.0.0.1:10101/api/v1 (v1)
     9021 belongs to elfldr, the ELF loader, which is ALWAYS running. Because this number used to
     be the "is it already up?" test as well, the check was permanently true and ShadowMount was
     therefore never actually auto-started by us - it only ever ran because the user autoloads it
     from Payload Manager. Same wrong port was reported as ShadowMount health.
     KEPT AT autostart 1, which is the behaviour this console has had for releases; the owner's
     autoload.txt starts it first in practice, and this only fires when nothing holds :10101. */
  { "shadowmount", "shadowmountplus.elf", 10101, 1, 0, pb_shadowmount, pb_shadowmount_end },
  { "pms-installer", "pms-installer.elf",     0, 0, 0, pb_installer,   pb_installer_end   },

  /* THE OWNER'S SET. Only ftpsrv auto-starts: it binds :2121 reliably, so the port test is a true
     "is it already up?" and starting it twice cannot happen. nanodns answers on UDP 53 and cannot
     be probed at all; kstuff, OnionHEN and the webkit installer change the jailbreak layer, which
     is the owner's call by the rule above; and Payload Manager is the thing that STARTS payloads -
     relaunching a live loader is how installs get wedged, so it ships and is never auto-started. */
  { "ftpsrv",      "ftpsrv.elf",           2121, 1, 0, pb_ftpsrv,   pb_ftpsrv_end   },
    /* 53/UDP, and `udp` is why it can be seen at all: nanodns answers no query sent to it from
     the LAN even while it is running, so the only observable fact is that its port is taken.
     autostart stays 0 - a second copy of a DNS server is not something to start unasked. */
  { "nanodns",     "nanodns.elf",            53, 0, 1, pb_nanodns,  pb_nanodns_end  },
  { "kstuff",      "kstuff.elf",              0, 0, 0, pb_kstuff,   pb_kstuff_end   },
  { "onionhen",    "onionhen.elf",            0, 0, 0, pb_onionhen, pb_onionhen_end },
  { "pldmgr",      "pldmgr.elf",           8084, 0, 0, pb_pldmgr,   pb_pldmgr_end   },
  { "webkit-autoloader-installer", "webkit-autoloader-installer.elf",
                                              0, 0, 0, pb_wkauto,   pb_wkauto_end   },
};
#define PAYLOAD_BUNDLE_COUNT 8

#endif

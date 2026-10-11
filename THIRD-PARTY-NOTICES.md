# Third-party notices

PKG MUTANT SHOP is licensed under the **GNU General Public License, version 3** (the file `LICENSE`
in this repository is the licence text). The CHANGELOG committed the project to "GPLv3 with source"
at the point the cheat engine was derived from CheatRunner, and the on-console ELF redistributes a
GPL-3.0 program (ShadowMountPlus), so nothing less permissive would be possible anyway.

This file lists what the two shipped artifacts carry or depend on that is not ours, read out of
`ps5-app/onconsole/payload_bundle.h`, `cheat_bundle.h`/`gen_cheat_bundle.py`, `jb.c`,
`companion/PKG-MUTANT-SHOP.spec` and the files themselves on 2026-09-04. Verified as part of
3.60.0: `PAYLOAD_BUNDLE_COUNT` was **2** (ShadowMountPlus and our own installer). **etaHEN is not
embedded** and nothing of Elf Arsenal is embedded.

**Re-verified 2026-09-30 for 3.87.0**, which added the Payloads & Homebrews panel: the PS5 ELF now
carries **8** payloads and the PS4 ELF **2**, listed below. They are embedded **unmodified** and
byte-identical to the copies the owner supplied, refreshed by `tools/sync_payload_bins.py`, and only
`ftpsrv` and ShadowMountPlus are ever started without being asked. The homebrew **packages** are not
embedded by anything - they are 264 MB and are copied to the console's own disk instead - so no
homebrew's licence is engaged by redistribution inside an artifact.

## Inside `PKG-MUTANT-SHOP.elf`

| Component | Origin | Licence | How it is used |
|---|---|---|---|
| **ShadowMountPlus** (`payloads/shadowmountplus.elf`) | drakmor - `github.com/drakmor/ShadowMountPlus` | GPL-3.0 | Embedded unmodified (`.incbin`, `payload_bundle.h`), written to `/data/pkg-mutant-shop/payloads/shadowmountplus.elf` on every boot and **auto-started** by the ELF when nothing answers on `:10101`. It is what mounts PS5/PS4 backups. Its source is not in this repository; obtain it from the project above. The shape of the ShellCore power-flag read in `server.c` was also taken from its `src/sm_shellcore_flags.c` (see CHANGELOG). |
| **pms-installer.elf** (`payloads/pms-installer.elf`) | ours - `ps5-app/onconsole/installer_probe.c` + `jb.c` | GPL-3.0 | Our install engine: embedded, written to `/data/pldmgr/payloads/pms-installer/` and spawned by Payload Manager once per install. Never auto-started. |
| **`jb.c` / `jb.h`** (the credential-escalation helper linked into both ELFs) | verbatim from **CheatRunner** by maj0r - `github.com/notmaj0r/CheatRunner`, `src/jb.c` | GPL-3.0 | Raises the process credentials so the system installer accepts our request. The cheat engine in `server.c` is also derived from CheatRunner's, with the project owner's authorisation (CHANGELOG, "Licence"). The old CheatRunner tree on a console is read only to migrate cheats out of it. |
| **pms-tile.pkg** (`tile/pms-tile.pkg`, `tile_bundle.h`) | ours - `ps5-app/tile-pkg/` | GPL-3.0 | The dashboard tile package (title `PKGM00001`). Built with LibProsperoPkg, which is not in this repository; the checked-in binary is currently the only copy. |
| **The cheat and patch library** (`assets/cheats`, packed by `gen_cheat_bundle.py` into `cheat_bundle.h` + `cheats.pack`; 7,022 files in the 3.60.0 ELF, fewer once the packer's `json`/`mc4`/`shn`/`patches`-only list is rebuilt) | copied from **TeeKay87/HEN-Cheats-Collection** - `github.com/TeeKay87/HEN-Cheats-Collection` (the folder `mods-cheats-patches-others`; the copy used here was taken 2026-08-02 and the patch archive inside it was built 2026-07-12) | **GPL-3.0** (that repository's `LICENSE`, the same text as ours) | Embedded and extracted to `/data/pkg-mutant-shop/cheats` on the console's first boot. See the note below on the files' own credits. |

### The payloads carried for the Payloads & Homebrews panel (3.84.0)

Embedded by `.incbin` (`payload_bundle.h` on the PS5, `payload_bundle_ps4.h` on the PS4), written to
`/data/pkg-mutant-shop/payloads` at boot, and sent to a loader only when the owner presses the tile -
except the two marked auto-start. Sources are the upstream projects; none is in this repository.

| Component | Origin | Licence | How it is used |
|---|---|---|---|
| **ftpsrv** (`ftpsrv-ps5.elf`, `ftpsrv-ps4.elf`) | `github.com/ps5-payload-dev/ftpsrv` | GPL-3.0 | Embedded unmodified in both ELFs. **Auto-started on the PS5** when nothing holds `:2121`; never auto-started on the PS4, which has no loader that can be asked. |
| **nanodns** (`nanodns.elf`, `nanodns-ps4.elf`) | `github.com/ps5-payload-dev/nanodns` | GPL-3.0 | Embedded unmodified in both ELFs. Never auto-started: it answers on UDP 53, which cannot be probed, so "is it already running?" has no honest answer. |
| **kstuff** (`kstuff.elf`) | part of the PS5 jailbreak chain | see upstream | Embedded unmodified. **Never auto-started** - it is the jailbreak layer, and `payload_bundle.h` records why that is the owner's call. |
| **OnionHEN** (`OnionHEN.elf`) | `github.com/w90008/onionHEN` | see upstream | Embedded unmodified. Never auto-started (jailbreak layer). |
| **Payload Manager** (`pldmgr_v0.5.2.elf`) | itsPLK - the binary names itself "Payload Manager v0.5.2 by PLK" | see upstream | Embedded unmodified. **Never auto-started**: it is the thing that starts payloads, and relaunching a live loader is how installs get wedged. |
| **WebKit autoloader installer** (`webkit-autoloader-installer_v0.5.0.elf`) | `github.com/itsPLK/ps5-webkit-autoloader` | see upstream | Embedded unmodified. Never auto-started (jailbreak layer). |


**Licences marked "see upstream"** are recorded that way deliberately rather than guessed: those
projects' terms were not read as part of this release, and writing a licence name we have not
verified into this file would be worse than saying so. Anyone redistributing this build should check
them. All six are redistributed unmodified and in full, which is the form every common licence is
most permissive about.

### The homebrew packages - NOT redistributed

`Itemzflow` (`ITEM00001`), `PS4-Xplorer` (`LAPY20009`), `FPKGi` (`PKGI13337`), `Internet Browser`
(`MOUU12023`) and `RetroArch` (`PPSA99169`) are **not inside any artifact**. The catalogue names
them, and the bytes are copied from the owner's own folder to their own console. Nothing about them
is distributed by this project.

### The cheat library - what is in it and whose work it is

The collection's `LICENSE` file was not copied into `assets/cheats` (and `gen_cheat_bundle.py`
would skip it by its `SKIP_NAMES` if it were), so the licence is recorded here: GPL-3.0, the GNU
General Public License version 3 exactly as in this repository's `LICENSE`. The files carry their
own credits, which are preserved unchanged inside the pack:

| Folder | Format | Credits, as the files carry them |
|---|---|---|
| `json/` (1,994) | JSON cheat files (`name`, `id`, `version`, `mods[]`) | the collection's authors; individual creators where the file names one |
| `shn/` (1,763) and `mc4/` (2,142, `.mc4` plus their `.mc4.xml` plaintexts) | Trainer XML | the `Moder="..."` attribute names the author of each file (e.g. Talixme) |
| `patches/` (376) - and, in the repo but no longer packed, `xml/` (369), `xml_orbis/` (369), `xml_prospero/` (9), whose files are byte-identical copies of `patches/` | game patch XML (`<Patch><Metadata Author="...">`) | generated from the **PS-Game-Patch** repository's `patches/xml*` sources (each file's first line records its origin and build time). `Author=` names the patch authors - most often **illusion**, also TL431, Jao, stagvant, BestPig, Lance McDonald (manfightdragon), phrost1338, ZEROx, foxyhooligans, SuleMareVientu, Nenkai, TheMagicalBlob and others. |

Nothing in the library was modified; the app reads them and applies them to a running game with its
own engine.

## Inside `PKG-MUTANT-SHOP.exe`

| Component | Licence | Notes |
|---|---|---|
| **Python 3** (the interpreter and standard library, frozen into the exe) | PSF License Version 2 | the companion is stdlib Python |
| **PyInstaller** bootloader (the exe's launcher stub) | GPL-2.0-or-later **with the bootloader exception** that permits distributing an executable built with it under any licence | pinned to 6.21.0 in `build_exe.cmd` |
| **Pillow** | HPND / MIT-CMU (Pillow's open-source licence) | box-art thumbnails (WebP) and the tray icon; the companion runs without it |
| **pystray** | LGPL-3.0 | the system-tray icon, imported only if present at build time; the companion runs without it |

`numpy` is deliberately excluded from the exe (`PKG-MUTANT-SHOP.spec`, `excludes`); nothing imports it.

## Toolchain (not shipped, needed to build the ELF)

| Component | Licence | Notes |
|---|---|---|
| **ps5-payload-dev SDK** (`~/sdk/ps5-payload-sdk`) by John Törnblom | GPL-3.0-or-later (each header: "either version 3, or (at your option) any later version") | headers, crt and libraries the ELF is compiled and linked against; fetched by `ps5-app/payload/build-wsl.sh` |
| **clang 18** (`~/clang18`, Ubuntu packages, `prospero-clang` wrapper from the SDK) | Apache-2.0 with LLVM exception | the compiler |

## What is NOT shipped, and why it is mentioned

- **etaHEN** - not embedded since 3.33.0, and **no longer in this repository at all**. A reference
  binary and a partial copy of its GPLv3 source used to be kept here for study; neither was ever
  part of either artifact, and redistributing somebody else's GPL binary from a public repository
  carries an obligation we have no reason to take on for a file nothing uses. Its source was read
  to understand why an install call fails from an injected process (see `installer_probe.c`); no
  code from it is used. `server.c`'s rest-mode list names the *process* only, to stand down a copy
  the user has started themselves.
- **Elf Arsenal** - not bundled, not started, not spoken to. Its GPLv3 source was read for the
  install mechanism only (CHANGELOG); no code from it is used.
- **Game Compressor** (juma-sayeh) - the `.ffpfsc` backups it produces are recognised by the backup
  lane; nothing of it is shipped.
- **Y2JB** - the user's own jailbreak; not bundled, not started, not spoken to.

  **Payload Manager and kstuff are NOT in this list, and used to be.** This file said the app
  "ships none of them" while the table above, sixty lines earlier, correctly recorded both as
  embedded - and `ps5-app/onconsole/payload_bundle.h` settles it: `PB_INCBIN(pb_pldmgr,
  "payloads/pldmgr.elf")` and `PB_INCBIN(pb_kstuff, "payloads/kstuff.elf")` link their bytes
  straight into the shipped PS5 ELF. They are redistributed, under their own licences, and are
  credited in the table. Neither is ever auto-started: the app requires Payload Manager to already
  be running, and starting either is the owner's call.

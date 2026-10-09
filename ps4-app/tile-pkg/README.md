# PKG MUTANT SHOP — the PS4 dashboard app

This builds **`IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg`**: a real, fake-signed PS4 application
package that puts the shop on the console's home screen.

Before this existed, a PS4 had nothing to press. The shop only existed while a payload happened to
be injected, and the jailbreak's binary loader does not survive rest mode — so after one suspend
there was no app on the console at all, whatever had been "installed".

```bash
bash ps4-app/tile-pkg/build-wsl.sh     # WSL; fetches its toolchain on first run
```

---

## Status — read this before believing anything below

| | |
| --- | --- |
| the package builds | **yes**, verified — `pkg_validate`: 28 checks `[OK]`, 0 errors, 0 warnings |
| the ELF carries it | **yes**, verified — found in the ELF, byte-identical, checked by a build gate |
| the ELF installs it on boot | **yes**, verified on 13.52 — from its own embedded copy, over the console's loopback |
| the package has been **installed** | **yes** — `/user/app/PKGM00001/app.pkg`, 6,619,136 bytes, with `app.pbm` / `app.json` / `app.xml` beside it |
| the ELF **updates** it | **yes**, verified — `01.00` → `01.01` → `01.02` → `01.03` on the hardware |
| the installed bytes are ours | **yes**, verified — read back over FTP and hashed: `01.03` = `5968c773…fb`, identical to the build |
| the icon is on the home screen | **yes** — listed by the console as *PKG MUTANT SHOP* |
| **the eboot has been run** | **yes** — as of 2026-09-21. It launches, runs, and opens the shop in the browser |
| why it used to fail | **our own install engine** released the BGFT task the title needs to launch. Fixed |
| what still needs work in the eboot | it cannot load system modules from inside the sandbox, and it exits with `SIGSYS` |

**The icon opens the shop.** It took two wrong diagnoses to get there, and both are worth knowing
about before touching anything in this folder.

`CE-32930-7` is `0x80990019` — and it comes from **`sceBgftNotifyGameWillStart`**, not from the mount:

```
[BGFT] [606] GameWillStart(PKGM00001, 2) start
[BGFT] ERROR: [3568] task not found. (PKGM00001)
sceBgftNotifyGameWillStart() ret = 80990019
[PFS] umount[…] finished 0                      ← rollback of a mount that SUCCEEDED
PrepareProcessLaunchPkg() ret = 80990019        ← only relaying it
mountApp0Dir: LNC_ISOK::0x80990019              ← only relaying it
```

The package's PFS images mount fine. ShellCore then asks BGFT to note that the game is starting,
BGFT has no **task** for the title, and ShellCore unmounts and gives up.

**The cause was `ps4-app/onconsole/server_ps4.c`, not this folder.** A finished BGFT task is the
title's launch ticket, and the shop's task housekeeping was handing it back after every install —
making every title it installed unlaunchable, games included. Leaving one task registered and
pressing the icon produced `[BGFT] [576] task(00000075) PKGM00001`, then `EXEC /app0/eboot.bin`, and
the app ran. The engine now keeps a task whose title has an `app.pkg` on disk and only reclaims one
when the console genuinely has no free slot.

**Two earlier explanations were confidently wrong**, and the reason is the same both times: a grep
keyed on the `<118>` klog prefix dropped the `[PFS] mount` and `sceBgftNotifyGameWillStart` lines, so
the first surviving line got read as the cause. The Riptide GP2 control test was over-read too — it
was installed by the same lane, so it never separated "fake-signed" from "installed by us". Verify
the instrument before believing the diagnosis.

**What the first successful launch exposed, and what 01.04 does about it:**

* `sceKernelLoadStartModule("/system/common/lib/…")` failed for every library — `-2147352574` is
  `ENOENT`, and inside the sandbox they are mapped at `/vm2LJNGVpN/common/lib/`, a per-sandbox random
  prefix. So `browser=0 user_init=0 notify=0`, and `HideSplashScreen` had never once run. **Fixed by
  linking**: `-lSceSystemService` makes `HideSplashScreen` and `LoadExec` real calls, and
  `sceKernelSendNotificationRequest` needed nothing at all — it is a libkernel export that `-lkernel`
  already provided.
* Returning from `main()` died with **`SIGSYS`** — the CRT's exit path reaching libkernel's `_exit`,
  a syscall a sandboxed application may not make. **Fixed structurally**: `_Exit` is defined in
  `main.c`, so libc's `_exit.lo` is never linked. `nm -u` has no `_exit`; `objdump -d -j .plt` has no
  `_exit@plt`. `main()` now ends with `sceSystemServiceLoadExec("exit", NULL)` and parks if refused.
* The app opens the shop by **asking the payload** (`GET /api/open` on `127.0.0.1:8710`, answered
  `200`). The payload is not sandboxed and this is the measured path. The app's own browser call is
  **deleted**: the toolchain declares `void sceSystemServiceLaunchWebBrowser();` — no url, no return —
  so the four-argument call it used to make was a guess.

**Not yet verified on hardware:** that pressing the icon now produces no dialog at all, and whether
`LoadExec("exit")` closes the app cleanly or it falls back to parking. That needs the icon pressed.


proven. The launch is blocked outside this repository.

## What it is

| | |
| --- | --- |
| title id | `PKGM00001` — the same identity the PS5 tile uses. One app, whichever console it is on, and it cannot collide with a game (those are `CUSA` / `NPXS`). |
| content id | `IV0000-PKGM00001_00-PKGMUTANTSHOP001` (`IV0000` is the homebrew publisher prefix) |
| category | `gd`, content type `0x1A`, flags `0x0A000000` — **byte-for-byte the shape of a retail base game** |
| contents | `eboot.bin`, `sce_sys/param.sfo`, `sce_sys/icon0.png` — **and nothing else** |

That last row is the point: it is an **ordinary PS4 package**. `companion/pkg_meta.py` reads it
exactly as it reads a retail title, our own integrity check calls it complete, and it installs down
the same lane every other game does. Nothing special-cases it.

## What pressing the icon does

1. Is the shop answering on `127.0.0.1:8710`? → open the console's browser at it.
2. If not, say so on screen: the button cannot conjure the server.

That is the whole program, and it is the same contract the PS5 tile has always had — the tile opens
the shop, the ELF *is* the shop.

## Why it carries nothing, and why the ELF carries it

The ELF installs this package, the way the PS5 ELF installs its tile. So this package must not
contain the ELF:

> The ELF would contain a package containing the ELF, and every rebuild would embed the previous
> one, growing without limit.

An earlier version did ship the payload inside, so that pressing the icon could start the shop by
handing the payload to a loader. It worked on paper and it is exactly the cycle above, so it is
gone. **One direction only: package first, then ELF.** Both build scripts say so, and
`ps4-app/onconsole/build-wsl.sh` refuses to build without the package.

### The version check

`ps4-app/onconsole/server_ps4.c` carries `PS4_TILE_VER` and, once its socket is listening, compares
it with the installed app's `APP_VER` from the console's own `tbl_appinfo`:

| console has | what happens |
| --- | --- |
| this version or newer, with its `app.pkg` on disk | nothing at all — not even a write |
| an older version | installed over it, which is how the PS4 updates a title |
| nothing | installed |

"Installed" is `app.pkg` **with bytes**, never an `app.db` row on its own — the rule this project
learned on the PS5, where trusting the row produced 53 phantom installs. Versions compare
numerically, so `1.00` and `01.00` are the same number and a leading zero cannot look older.

The staged copy goes to `/data/pkg-mutant-shop/pms-tile.pkg` — **our own folder**. Nothing this app
does touches the jailbreak's files or folders; the jailbreak's only job is to start the ELF.

### Nothing privileged

A loopback connect, its own `/app0`, two public system-service calls. An application runs sandboxed
— it cannot read `/data` and it cannot inject code into another process — so a button that needed
either would be a button that does not work.

## The toolchain

Everything comes from the [OpenOrbis PS4 Toolchain](https://github.com/OpenOrbis/OpenOrbis-PS4-Toolchain)
v0.5.4, which `build-wsl.sh` fetches on first run:

* `clang` (FreeBSD 12 x86-64 target) → an ELF
* `create-fself --paid 0x3800000000000011` → `eboot.bin`, a signed fself
* `PkgTool.Core sfo_new` / `sfo_setentry` → `param.sfo`
* `create-gp4` → the project file
* `PkgTool.Core pkg_build` → the package

The build also unpacks **libssl 1.1 beside the toolchain**: PkgTool's bundled .NET runtime needs it,
this machine has libssl 3, and there is no root here. Extracting the `.deb` locally and pointing
`LD_LIBRARY_PATH` at it keeps the whole build in one script and touches nothing outside that folder.

The `Makefile` is adapted from the toolchain's own `hello_world` sample rather than invented, so the
steps are the ones its authors document.

### Checked, not assumed

* the built `.oelf` is `SCE Executable (ASLR) - 0xFE10` with a FreeBSD ABI — what a PS4 eboot is
* extracting the finished package shows the payload inside at its exact byte count
* `pkg_meta.parse_pkg` reads back the title, title id, content id, category and version
* `pkg_meta.pkg_completeness` calls it complete
* the toolchain's own `hello_world` was built and packaged first, so the chain was proven on
  something known-good before ours was trusted

## If it goes wrong — the way out

**Try these in order. The first one is almost certainly enough.**

1. **Delete it from the home screen.** Highlight the icon → Options → Delete. This is the normal
   path and it is available: every package our lane has installed on this console carries
   `canRemove = 1` in `app.db`, so the menu item is not greyed out.
2. **Over FTP**, if the icon is there but will not delete: remove `/user/app/PKGM00001` and
   `/user/appmeta/PKGM00001`. This leaves a row behind in `app.db`; on this console a row with no
   data behind it is already demonstrably tolerated — `CUSA00001` "THE PLAYROOM" has sat like that
   without consequence — but it is still untidy, not a fix.
3. **`sceAppInstUtilAppUnInstall("PKGM00001")`** — the canonical signature, from the toolchain's own
   `AppInstUtil.h`. **`dlsym` it before calling it.** On this firmware `libSceBgft`'s older names
   are absent and only the `ServiceInt` family resolves, so nothing here is safe to call on the
   strength of a header. Guessing a system call's shape is what crashed a console on the PS5 side.

**Do not use Rebuild Database.** On a jailbroken PS4 it runs outside HEN and drops every fake-package
entry from the home screen — it would take Riptide GP2, Metal Slug XX and Call of Duty with it.

The failure that cost a console on the PS5 side was a **registration with no data behind it**: a
title written into the database by hand, which the system then tried to launch as a game. This is
the opposite — a complete package handed to the console's own installer, which is the only thing
that writes those records correctly. That is why the package route was taken and the shortcut was
not.

The failure that cost a console on the PS5 side was a **registration with no data behind it**: a
title written into the database by hand, which the system then tried to launch as a game. This is
the opposite — a complete package handed to the console's own installer, which is the only thing
that writes those records correctly. That is why the package route was taken and the shortcut was
not.

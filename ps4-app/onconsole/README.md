# PKG MUTANT SHOP — the PS4 payload

This is the PS4 half of the shop. It serves the same web UI and speaks the same JSON API as the PS5
payload (`ps5-app/onconsole/server.c`), so the companion and the page need no per-console code paths
beyond knowing which console they are talking to.

Verified on real hardware: **PS4 firmware 13.52**, jailbroken with GoldHEN. Everything in this document was
measured on that console. Nothing here is inferred from the PS5 side.

> A note on the GoldHEN version. This document used to say "GoldHEN 2.4b18". That release covers
> firmware 5.05 through 11.00 and cannot be what is running here — 13.52 support arrives in the
> `2.4b18.x` point builds. The exact build on this console has **not** been read off the screen;
> its FTP banner only says `GoldHEN FTP server v2.2`, which is the FTP module's version and not
> HEN's. So nothing in this project should gate behaviour on a GoldHEN version string, and nothing
> does — every symbol is `dlsym`-checked before it is called, which is why the `ServiceInt` finding
> below held up regardless of which point build this is.

---

## One binary for both consoles is not possible

This was tested rather than assumed, because it was the first thing worth knowing.

The PS5 ELF was posted to the PS4's payload loader. GoldHEN accepted it — HTTP 200, *"payload
launched successfully", "format: ELF"* — and **nothing ran**: port 8710 never opened and klog stayed
silent. The two consoles have different SDKs, different ABIs and different system libraries, and a
PS5 payload on a PS4 is simply not a program that machine can execute.

So there are two payloads and **one** of everything else: one web UI (embedded in both from `web/`),
one companion, one API, one set of build gates. That is the part that matters to anyone using the
app — the shop looks and behaves identically on both consoles.

## Building

```bash
bash ps4-app/onconsole/build-wsl.sh        # WSL; writes PKG-MUTANT-SHOP-PS4.elf beside itself
```

It fetches and patches the SDK the first time (see below), then runs **the same gates the PS5 build
runs** — `check_web.py`, `i18n_report.py --check`, `message_report.py --check` — because both
payloads embed the same UI and say the same sentences, so a broken script or an untranslated key has
to stop both builds, not one.

### The SDK has to be patched or `main()` never runs

The payload is built with the [ps4-payload-dev SDK](https://github.com/ps4-payload-dev/sdk). Its
C runtime assumes it is running under a loader that provides kernel access, and GoldHEN does not:

* `payload_init()` calls `__kernel_init()`, which calls `kexec(...)` — a syscall GoldHEN does not
  provide — and treats the failure as fatal.
* `__rtld_init()` escapes the process jail before loading libraries. With the kernel unreachable
  that escape cannot happen.
* `__rtld_init()` then *restored* the jail unconditionally, failed, and returned −1 **after** every
  library had already loaded fine. This was the last blocker and the least obvious one.

`sdk-goldhen.patch.py` makes those three kernel steps advisory and is applied by the build script
automatically. It is idempotent and each patch carries its own marker — an earlier version shared
one marker between patches, so the third silently never applied and looked "already patched".

Symbol resolution stays fatal: a payload that cannot find what it needs must refuse, not run blind.

**Consequence:** with `kexec` absent there is **no kernel read/write** on this console, so the
PS5's CR3-page-walk cannot be ported as it stands — and neither userland alternative is open
either: `mdbg` (syscall 573) returns **EPERM** and `ptrace` returns **EPERM before it even looks
the target up**, both measured from this payload on 13.52.

**So the engine runs INSIDE the game instead.** `ps4-app/plugin/` is a small GoldHEN plugin
(`pms-agent.prx`) that GoldHEN's own `plugin_loader` puts into every game; it serves four
requests — ping, info, read, write — on `127.0.0.1:9231`, and knows nothing about cheats, files or
titles. The engine itself is unchanged and shared: `tools/ps4_sync_cheat_core.py` copies it out of
`ps5-app/onconsole/server.c` into `cheat_core.h`, and both ELF builds run that tool with `--check`
so the two cannot drift. `mem_read`/`mem_write` are the only things that differ between the
consoles. The plugin travels inside this ELF (`agent_bundle.h`) and is written to the console at
boot, so there is nothing for an owner to copy.

`/api/cheat/*`, `/api/mods/*`, `/api/patch/*` and `/api/mem/*` are all answered here now. Toggling
needs the helper to be inside the running game — a game started before the helper was listed is
running and cannot be written to, and the refusal says exactly that. The protocol between the two
halves is tested on a PC with no console at all by `tools/test_agent_protocol.py`, which compiles
both shipped sources and runs them against each other; `ready_check` gates it.

**There is no way to start a game from this payload.** `sceLncUtilLaunchApp` is the only launch
call with a real signature in the toolchain and it does not resolve here (no `libSceLncUtil.sprx`
in `/system/common/lib` or `/system/priv/lib`); `sceSystemServiceLaunchApp` resolves but is
declared `void f()`, so calling it would mean inventing its arguments. That means the last step of
the chain — a cheat landing in a running game — is verified by a person starting a game.

## What the payload gets under GoldHEN

GoldHEN injects it into a **shared host process** (`ScePartyDaemon`, pid 66) which it jailbreaks
first, so the payload inherits **uid 0 / gid 0** and can read the whole filesystem.

That word *shared* has one hard consequence: **never call `_exit()`.** It tears down the host
process — including the copy of the payload that was just loaded to replace this one. `/api/quit`
closes the listening socket and returns instead, so a new build takes the port over cleanly.

Working, measured: root, TCP listen + HTTP, `sceKernelSendNotificationRequest` (the same 3120-byte
struct as the PS5), `dlopen`/`dlsym`, `sceKernelLoadStartModule`, `sceAppInstUtilInitialize()` → 0.

## Installing — BGFT

A package is installed by handing the console a URL and letting it download and install by itself,
with its own progress UI and its own notifications. `bgft.h` carries the declarations, copied from
the OpenOrbis toolchain rather than inferred, and every symbol was confirmed to exist on this
console before it was called.

Firmware detail that matters: **on 13.52 `libSceBgft` exports the `ServiceInt` family**, and the
register call is the *Debug* one — `sceBgftServiceIntDebugDownloadRegisterPkg`. The older
`sceBgftInitialize` / `sceBgftDownloadRegisterTask` names most PS4 installers use are absent here.

Resolution goes through `dlsym_any()`, which searches the handles we `dlopen` as well as the default
scope. **`RTLD_DEFAULT` alone is not enough**: every BGFT symbol came back NULL from it and resolved
fine from the library handle, and the first build of this file refused every install because of it.

Three things the task will not do without:

| field | why |
| --- | --- |
| the **content id** (`EP0786-CUSA02365_00-…`) | a title id here is refused with `0x80990008` |
| the real **byte size** | zero is refused outright |
| the **package type** (`PS4GD`/`PS4GP`/`PS4AC`) | from the package's own `param.sfo` CATEGORY |

The console cannot read a header it has not downloaded, so for a package on the PC the companion
supplies these (it parsed the header when it scanned the library); for a package already on the
console the payload reads them out of the file itself.

### Tasks have to be handed back

BGFT keeps one directory per registered task under `/user/bgft/task` and the table is **not**
unlimited. A task only disappears when something unregisters it, so after a handful of failed test
installs every new one was refused with `0x80990086` while the dead tasks sat there.

**That was read as "the table is full", and it is not.** klog prints the name beside the code:
`0x80990086` is `SCE_BGFT_ERROR_CONTENT_ALREADY_DOWNLOADING`, and `0x80990088` is
`SCE_BGFT_ERROR_SAME_APPLICATION_ALREADY_INSTALLED`. **The conflict is per content id.** Releasing
tasks only ever looked like making room because the task released was the one holding that title's
id. Measured since: registering works fine with thirteen directories in `/user/bgft/task`.

So `bgft_release_title()` releases the tasks of the title about to be installed, and nothing else —
every other title keeps its task, because that task is what lets it launch (see **what proves an
install**). If the refusal survives that release, the task in the way belongs to the console itself
and is not ours to remove; the shop says so instead of deleting it.

A task counts as ours only when its record (`d0.pdb`, plain text) carries a **plain-http URL on one
of the two routes we own** — `/library/` on a companion, or `/pkgfile/` on this console itself — and
a store task is https on a Sony host. Both halves must match. The seven tasks this console already
had from the user's own Store and firmware downloads were left untouched, and one of those was plain
http, which is exactly why one test is not enough. Matching only `/library/` (as the first version
did) left every task from a local install invisible to the sweep — the exact leak it exists to fix.

### The one thing BGFT will not do: a title the Store has an update for

Measured, twice, on this console. Before downloading anything, BGFT asks PlayStation Network whether
the title has a newer version:

```
[ScePatchChecker] check (title_id='CUSA11740', …): status=0   -> installed fine, 15 s
[ScePatchChecker] check (title_id='CUSA14409', …): status=1   -> app_version 01.04 exists
[PATCH MERGE] : CheckDeltaPatchInfo error. [0x80f00640]
[BGFT] tx started (131072/238419968)     <- 227,540,992 offered + 10,878,976 it could not fetch
[BGFT] tx stopped  (524288/10878976)
Task ended (error=0x80990004)
```

When the Store has a newer version, BGFT builds a **two-part** task — the package we offered *plus*
the update — and then cannot fetch the second part, because the first did not come from the Store.
No task option changes this: `FORCE_UPDATE`, `INTERNAL`, `REMOTE`, `INVISIBLE` and
`entitlementType` 0–3 were each tried and all four failed identically.

So the shop says so, in words, and only when it can prove it: the job reports the merge attempt
**only** if BGFT asked for more bytes than the package contains and nothing landed on disk. Anything
else gets the plain sentence — the cause is never guessed. Stopping the console from reaching
PlayStation Network is what clears it.

## Installing a package the console already has

`/api/engine/install-local?path=…`, or `/api/install` with `{"install_key":"local:<path>"}` — the
same shape the PS5 build takes. Packages found on `/mnt/usb0…7` are listed in `/api/library` with
`source: "usbN"`, so a stick is installable from any device with every PC switched off.

The package is **served back to the console over HTTP** (`/pkgfile/<token>`, with byte ranges) and
that URL is handed to the same BGFT lane. It is not registered with `sceAppInstUtilAppInstallPkg`:
on the PS5 that call writes metadata without data and leaves a tile that crashes the console on
launch, and the shape that avoids it is the same on both machines. One lane, one verdict path.

## Reading what is installed

`app.db` is at the same path as on the PS5 (`/system_data/priv/mms/app.db`) and has a **different
schema**: per-user `tbl_appbrowse_<userid>` (titleId, contentId, titleName, contentSize, category, …)
plus a key/value `tbl_appinfo`. Category `gd` is a real game, `gdi` a system stub. The installed
version is `tbl_appinfo` key **APP_VER**, which is the field the companion already prefers for CUSA
titles. `sqmini.h` is the PS5 payload's SQLite reader, copied verbatim and kept honest by
`tools/ps4_sync_sqmini.py --check` (a build gate) rather than edited by hand.

**There is one `tbl_appbrowse_` table per user and the payload reads the first one.** That was
checked rather than assumed: on a console with two users (`tbl_appbrowse_<user id>`, one table per
user) the two tables held 79 and 66 rows, and filtering both to real games gave **the same titles**
with nothing in one and not the other - the PS4 registers an installed game for every user. Reading
the first table therefore loses nothing, and a union pass would be more code for no measured gain.
If a title ever does turn up for one user only, this is the line to revisit.

`addcont.db` is also readable and carries a `content_id` column, so the companion's DLC proof
(`installed_addons`) works on this console with no change - checked against the live file.

Installed means **`/user/app/<TID>/app.pkg` with bytes in it** — never app.db registration alone.
That rule was learned on the PS5, where trusting metadata produced 53 phantom installs.

`/user/bgft` here is directories (task, trash, pushlist, userlist), not the PS5's `bgft.db`, so the
PS5's "bgft row 1026/1036" confirmation has no equivalent — the app.pkg check is the proof.

## The dashboard app

There is one: **`ps4-app/tile-pkg/`** builds `IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg`, a real
fake-signed PS4 application that puts the shop on the home screen. Read that folder's README for
how it is built and why it is shaped the way it is.

Two things about it matter here, and they pull against each other:

* **It carries this payload**, so pressing the icon can start the shop with every PC switched off.
  It hands the payload to the jailbreak's binary loader on `:9090` and then opens the browser.
* **This payload carries it back**, and installs it itself - over the console's own loopback, from a
  copy embedded in the ELF, with no PC, no FTP and nothing written into any jailbreak folder.

Both at once is a package inside a package inside a package, growing with every rebuild. **The lite
build cuts the cycle.** `build-all-wsl.sh` builds three artefacts in a line, never a circle:

| | | |
| --- | --- | --- |
| 1 | `PKG-MUTANT-SHOP-PS4-LITE.elf` | the same shop, with no package inside it (~1.7 MB) |
| 2 | `IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg` | the home-screen app, carrying **1** (~6.6 MB) |
| 3 | `PKG-MUTANT-SHOP-PS4.elf` | the shop, carrying **2** (~8.3 MB) |

Lite gives up exactly one thing - installing the home-screen app - and whoever is running lite got
there by pressing that app, so it is already installed. A build gate refuses to build when
`PS4_TILE_VER` and `ps4-app/tile-pkg/Makefile`'s `VERSION` disagree, because `"01.00"` and `"1.00"`
normalise to the same number and every fix shipped in the package would otherwise be invisible.

### What proves an install, and what does not

**`/user/app/<TID>/app.pkg` CHANGING is the proof. Its mere existence is not.**

That distinction cost a live install. `job_refresh()` used to call a job finished as soon as that file
was present at roughly the expected size - honest for a first install, worthless for an update, where
the file is already there from the version being replaced. So the first poll after an update started
declared success and `bgft_release()` stopped and unregistered the *running* task. The console's own
log: `tx stopped (524288/6619136)`, `error=0x0`. It left `app.db` carrying the new version number
while `app.pkg` still held the old bytes, because the console writes the version early and promotes
the file last, in `AppInstallApp`.

So every install now records `app.pkg`'s size **and mtime** before it starts (`app_pkg_facts()`,
`job_baseline_locked()`) and nothing is finished until that file changes. And because a version number
cannot detect the state that cancellation left behind, `tile_bytes_match()` compares the installed
package against the copy inside this very ELF, 64 KB at a time, and reinstalls when they differ -
bounded to one attempt per build by `/data/pkg-mutant-shop/tile-repair.stamp`, so a firmware that ever
re-wrapped `app.pkg` could not turn this into an install on every boot.

`install_path_cleanup()` then removes every `pms-tile*.pkg` under `/data/pkg-mutant-shop` - only after
the install is proven, only in that directory, only that name pattern. `/api/tile/status` reports on
all of it, from `app.pkg` on disk rather than an `app.db` row.

### Console-owned transfer tasks are never touched

`bgft_sweep_ours()` releases a task only when that task's own `d0.pdb` contains a plain-http URL on
`/library/` or `/pkgfile/`. A system firmware task points at `dus01.ps4.update.playstation.net` and a
Store task is https on a Sony host, so neither can ever match. **Nothing in this project starts,
resumes or cancels a transfer that is not ours** - a firmware download least of all, since installing
one would cost the jailbreak. Audited on the hardware: of 11 slots in use, 9 were the console's own
(8 for one title, plus a `PS4UPDATE.PUP`), and only our 2 were released.

`/api/open` remains, and is what the app uses once the shop is up: it launches the console's own
browser at the shop, so one tap from anywhere puts it on the television. The URL is also shown in
Settings.


## Routes the PS5 has and this console does not

Unknown `/api/` paths fall through to a bare `{}` so an older page keeps working - which is exactly
what makes a missing route dangerous, because the caller reads `{}` as "nothing to report" rather
than "this console does not do that". Every route the companion actually calls was checked against
this payload, and each one the PS4 cannot do now answers in words: cheats, mods and patches (GET as
well as POST - the GET side was answering `{}` to the settings panel), backup move and delete, and
payload autostart. `/api/rest/prepare` answers that there is nothing of ours to stop, and that the
shop has to be loaded again after the console wakes, which was measured rather than assumed.

The one deliberate exception is **`/api/helpers`**. On the PS5 it reports ShadowMount, which binds
loopback-only so only the console can see it. A PS4 has no ShadowMount and its FTP belongs to the
jailbreak, not to us - so the honest answer is silence: the companion only overlays a helper state
when the console replies `ok: true`, and otherwise paints "unknown" instead of a red "not running".

## Ports and tools on the console

| port | what |
| --- | --- |
| 8710 | this payload — the shop UI and the JSON API |
| 2121 | GoldHEN's FTP (logs in as root, whole filesystem) — the companion's fallback transport |
| 3232 | klog. **The only way to see payload output**; `printf` goes nowhere when injected |
| 9090 | GoldHEN's payload loader — HTTP POST the raw ELF; 200 means accepted |

Deploy: `curl -X POST --data-binary @PKG-MUTANT-SHOP-PS4.elf http://<ps4>:9090/`, wait ~12 s, then
check `/api/health`. Reloading over a running copy is fine — the old one hands the port over.

Note that **9090 does not survive rest mode**: after a suspend/resume, FTP and klog come back and
the payload loader does not, so the exploit has to be re-run before another ELF can be pushed.

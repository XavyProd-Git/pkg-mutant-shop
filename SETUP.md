# PKG MUTANT SHOP 3.94.0 - setup and runbook

This is the one current document for installing, running, updating and triaging the app. Every
sentence in it was read out of the code as it is today (`companion/server.py`,
`ps5-app/onconsole/server.c`, `ps5-app/onconsole/installer_probe.c`, `payload_bundle.h`). Where an
older document disagrees with this one, this one is right. For how the pieces fit together see
[ARCHITECTURE.md](ARCHITECTURE.md).

Three artifacts, nothing else:

| Artifact | Runs on | What it is |
|---|---|---|
| `PKG-MUTANT-SHOP.exe` | a Windows PC | the companion: scans your game folder, serves the packages over HTTP, runs the install queue, serves the UI |
| `PKG-MUTANT-SHOP.elf` | the PS5 (jailbroken, firmware up to 13.60) | the on-console server on `:8710`: the UI, the install engine, the cheat engine, the dashboard tile, and the helpers it carries inside itself |
| `PKG-MUTANT-SHOP-PS4.elf` | the PS4 (FW 13.52, GoldHEN) | the same server and the same UI on `:8710`, plus the home-screen application it installs itself |

Nothing has to be downloaded separately. Each ELF embeds the UI, the helper payloads, the homebrew
catalogue and the whole cheat/patch library; the PS5's also carries ShadowMountPlus, its own
installer (`pms-installer.elf`) and the dashboard tile package.

**The folders are made for you.** On startup the companion creates its games folders and its
payloads folder (`Payloads/PS4`, `Payloads/PS5`, `Homebrews/PS4`, `Homebrews/PS5`) if they are not
there. Nothing has to be created by hand.

---

## 1. PC side

### 1.1 Files

Put `PKG-MUTANT-SHOP.exe` in a folder of its own. It reads and writes **beside itself**:

```
PKG-MUTANT-SHOP.exe
config.json          settings - created and rewritten by the app (auto-found console, Settings panel)
installed.json       what it has confirmed installed
pms.log              the companion's log (rotated)
.cache\              extracted box art and card thumbnails
```

The exe does **not** read `companion/config.json` from the repo. That file is only used when
running from source (`python companion/server.py`). If you copy only the exe to a new PC, it starts
on defaults and writes a fresh `config.json` next to itself.

### 1.2 The games folder

```
C:\Mutant Games\PS4\    PS4 packages (.pkg) - base games, updates, DLC
C:\Mutant Games\PS5\    PS5 packages (.pkg) and PS5 backups (.ffpfsc / .ffpkg / .ffpfs / .exfat)
```

The app creates this tree on first start and always scans it; it is inserted at the front of
`library.local_paths` and cannot be removed. Extra folders can be added in Settings or under
`library.local_paths` in `config.json`. Drop a file in and the library picks it up within about ten
seconds (a package is renamed to a URL-safe name read from its own `param.sfo` before it is listed).

### 1.3 Ports

| Port | Protocol | Who listens | Used for |
|---|---|---|---|
| **8710** | TCP | the exe | the UI, `/api/*`, and `/library/<key>` - the file server the **console downloads packages from** (HTTP Range, resumable) |
| 9097 | TCP | the exe | the console's log relay (the PS5 pushes its log lines here) |

Windows Firewall must allow inbound connections to the exe on 8710 (private network). If it does
not, the console reports `0x80B22404` on every install: it could not fetch the file.

### 1.4 Starting it

Double-click the exe. It

1. creates `C:\Mutant Games\PS4|PS5` if missing and scans the library,
2. looks for the PS5: the saved `ps5_ip`, else a sweep of the local `/24` asking each host's
   `:8710/api/health` for `on_console:true` (a host that will not confirm it is a console is used for
   this run only and never saved),
3. binds `0.0.0.0:8710`, opens `http://localhost:8710` in your browser, and sits in the system tray
   (right-click - Open / Quit),
4. announces itself to the console every 8 s (`/api/register-pc`) so the page served by the PS5
   knows where the packages are.

Running from source instead: `python companion/server.py` (Python 3.8+, stdlib; **Pillow** is used for
card thumbnails and the tray icon, both degrade gracefully without it). `start.cmd` does the same
and creates `companion/config.json` from `config.example.json` the first time.

### 1.5 A second PC (federation)

Run the exe on the other PC with its own `C:\Mutant Games`. Nothing to configure: each companion
sweeps the LAN for others at start and every five minutes, and re-checks the ones it knows every
20 s. The library you see is the union, deduplicated by title id, and a game that lives on the other
PC is installed by the console **straight from that PC** - nothing is proxied. A PC on another
subnet or behind a tunnel can be listed by hand under `federation.peers` (`"http://host:8710"`).
The console page picks, among all PCs announcing themselves, the newest build first.

### 1.6 The config keys that matter

| Key | Default | Meaning |
|---|---|---|
| `ps5_ip`, `consoles[].ip` | auto-found | the console. `consoles[]` also takes `name` and `ftp_port` |
| `dpi.pldmgr_port` | `8084` | Payload Manager on the console - the only external requirement of an install. (The section keeps its old `dpi` name so old files still parse; `mode`, `host`, `port_v2`, `port_v1`, `port_ezremote`, `auto_reload`, `live_probe` are ignored) |
| `ftp.port` | `2121` | first FTP port tried; 1337 is tried next and whichever answers is remembered. FTP is optional - only `deploy.py elf` and a few diagnostics use it |
| `shadowmount.port` | `10101` | where ShadowMountPlus reports (loopback-only on the console; the PC only relays what the ELF says). **Not 9021** - that is elfldr |
| `shadowmount.scan_path` | `/data/homebrew` | where a backup sent to "internal" lands; other drives are `/mnt/<drive>/homebrew` |
| `library.local_paths` | `["C:\\Mutant Games"]` | folders scanned |
| `queue.max_parallel` | `2` | transfers in flight across consoles; per console it is always one at a time |
| `companion.host` / `port` | `0.0.0.0` / `8710` | keep `0.0.0.0` - the console has to reach it |
| `integrity.on_local_corrupt` | `"warn"` | `"block"` refuses to hand over a package whose PFS image does not fit |
| `federation.peers` | `[]` | hand-listed companions, in addition to the discovered ones |

---

## 2. Console side

### 2.1 What has to be running first

The jailbreak, then Payload Manager. On this firmware that is:

```
Y2JB (from the YouTube app)  ->  the autoloader  ->  pldmgr (Payload Manager, web page + API on :8084)
```

Payload Manager is the **only** thing the app requires. It is how the ELF gets loaded, and it is
what spawns our installer for every install. `kstuff_lite` and `shadowmountplus` are normally in the
autoloader's list as well; the ELF starts ShadowMountPlus itself if it is not already up.

Nothing else needs to be, or should be, running: no DPI host, no install daemon, nothing on 12800.

### 2.1b If the console is a PS4

A PS4 on 13.52 needs its own payload, `PKG-MUTANT-SHOP-PS4.elf` - the PS5 one will not run there,
and the jailbreak's loader accepts it and then does nothing, which looks like a crash but is simply
the wrong machine. Post it to the loader the jailbreak provides:

```
curl -X POST --data-binary @PKG-MUTANT-SHOP-PS4.elf http://<ps4>:9090/
```

Wait about twelve seconds, then `http://<ps4>:8710/api/health` should answer with
`"platform":"ps4"`. Put that address in **Settings -> Your consoles -> PS4 address** and the console
joins the picker. Reloading over a running copy is fine: the old one hands the port over.

**A PS4 loses its payload loader in rest mode.** After a suspend and resume the jailbreak's FTP and
klog come back and the loader on 9090 does not, so the exploit has to be re-run before another ELF
can be pushed - and the shop has to be loaded again too. `/api/rest/prepare` says so rather than
pretending there is something to stop.

There is no `deploy.py` for the PS4: that script drives Payload Manager, which is a PS5 tool, and
this app does not reach into the PS4's jailbreak.

### 2.1c The PS4's home-screen app

The PS4's home-screen app is a real PS4 application that puts the shop on the console's home
screen, so it opens from an icon instead of only existing while a payload is loaded.

**It is not a file you download.** There are three artefacts and no more: `PKG-MUTANT-SHOP.exe`,
`PKG-MUTANT-SHOP.elf` for the PS5 and `PKG-MUTANT-SHOP-PS4.elf` for the PS4. The application's
package is carried *inside* both the PS4 ELF and the exe, so whichever of them is running can put
the icon on the console without anything else being present.

**The PC installs it for you** the first time it sees a PS4 that does not have it, and Settings ->
Your consoles -> **PS4 home screen app** shows its state with a button to put it back. That row
shows a dash while the PS4 is switched off: the app does not guess about a console that cannot
answer.

**Without a PC**, install it from the console's own package installer - the file lives beside the
exe and can be copied to the PS4 over FTP into whichever folder that installer scans.

Pressing the icon starts the shop by handing the payload it carries to the jailbreak's loader, so
**the jailbreak has to be running** - the same as every other homebrew on a PS4. After a sleep or a
power cycle, run the jailbreak again and then press the icon.

To rebuild it: `bash ps4-app/tile-pkg/build-wsl.sh` (fetches the OpenOrbis PS4 toolchain on first
run). It embeds whatever `ps4-app/onconsole/PKG-MUTANT-SHOP-PS4.elf` currently is, so build the
payload first.

### 2.2 Loading the shop

The registered path Payload Manager runs is

```
/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf
```

Put the ELF there (FTP, uploaded as `.part` and renamed, or let `python companion/deploy.py elf` do
it - section 5), then load it from Payload Manager's page or with
`GET http://<ps5>:8084/loadpayload:/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf`.
Payload Manager resolves `/loadpayload:` **by basename against its own registered copy**, so that
exact file is the one that has to be replaced - a copy uploaded anywhere else loads the old build
and says OK.

The ELF is not persistent across reboots: load it again after each boot, or add it to Payload
Manager's autoload list (`/data/pldmgr/autoload.txt`).

### 2.3 What the ELF does when it starts

In this order:

1. raises its own credentials (root, sandbox escape) so `/data`, `/mnt/ext1` and the system
   databases are reachable;
2. writes the embedded UI to `/data/pkg-mutant-shop/web` (every boot, so a new ELF is a new UI);
3. starts extracting the cheat/patch library to `/data/pkg-mutant-shop/cheats` (several thousand
   files on the first boot; later boots write only what is missing);
4. binds `:8710`. If an older copy of the shop still owns the port it asks it to quit
   (`/api/quit`) and takes over;
5. toasts **"PKG MUTANT SHOP v<version> is ready - Open it from Media, or <ip>:8710 in any browser"**.
   From this moment installs are possible: the engine needs nothing running beforehand;
6. in the background: if the dashboard tile is missing it installs `pms-tile.pkg` (embedded; written
   to `/data/pkg-mutant-shop/pms-tile.pkg` and handed to the console's own installer) - the tile
   appears under **Media** with no reboot;
7. still in the background, after a delay (`/data/pkg-mutant-shop/autostart-delay`, seconds,
   default 30, max 600), once Payload Manager answers and a user is signed in: writes its helper
   payloads to `/data/pkg-mutant-shop/payloads/` and starts the two that are marked to auto-start:
   **ShadowMountPlus** (`shadowmountplus.elf`, only if nothing answers on `:10101` yet) and the
   **FTP server** (`ftpsrv.elf`, only if nothing answers on `:2121`). The other six are written and
   left alone - `pms-installer.elf` is spawned per install, and kstuff, OnionHEN and the WebKit
   autoloader change the jailbreak layer, which is the owner's call and never a side effect.
   Create `/data/pkg-mutant-shop/no-autostart` to skip this step entirely.

### 2.4 Where things live on the console

```
/data/pkg-mutant-shop/
    web/                     the UI (rewritten every boot)
    cheats/                  json/ mc4/ shn/ patches/ ... - the cheat and patch library
    payloads/                the eight helper payloads the ELF carries (rewritten every boot)
    install/                 staging for console-local transfers
    pms-tile.pkg             the dashboard tile package
    installer-req.txt        one install's request (URI, display name, token) - consumed by the installer
    installer-res.json       that install's verdict - read once, then cleared
    install.log              every install, with its result code - survives a crash (O_SYNC)
    power.log                suspend/resume observations
    no-autostart             flag: do not start helpers
    autostart-delay          flag: seconds to wait before starting helpers
/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf     what Payload Manager runs
/data/pldmgr/payloads/pms-installer/pms-installer.elf        our installer, written per install
/data/homebrew, /mnt/<drive>/homebrew                        ShadowMount watch folders (backups)
/system_data/priv/mms/bgft.db                                the installer's own ledger (proof - section 4)
/system_data/priv/mms/app.db                                 the dashboard's registry (never proof on its own)
```

---

## 3. Phone, tablet, TV

| Open | What you get |
|---|---|
| `http://<ps5>:8710` | the page from the console. It reads `on_console:true` from its own `/api/health`, then asks `/api/companion` for the PCs that announced themselves and re-points its API at the newest one. Installs from a PC work from here. With no PC on, it runs console-only: what is installed, backups already on a drive, cheats and patches - but a package that lives on a PC cannot be installed |
| `http://<pc>:8710` | the same page from the PC; everything comes from the PC |
| the **PKG MUTANT SHOP** tile under Media | opens `http://127.0.0.1:8710/` in the PS5's browser - the first row above |

One page for every device: PS5 WebKit, PC browsers, iPhone Safari. It is plain ES5 on purpose.

---

## 4. How an install actually flows

```
Install pressed (any device)
  -> the PC's queue takes the job (one running install per console; two transfers across consoles)
  -> the PC checks the package fits its own PFS image (warns, does not block, by default)
  -> GET http://<ps5>:8710/api/engine/install-spawn?uri=http://<pc>:8710/library/<key>&name=<title>
  -> the ELF writes /data/pkg-mutant-shop/installer-req.txt (URI, name, a per-request token),
     writes its embedded pms-installer.elf to /data/pldmgr/payloads/pms-installer/, and asks
     Payload Manager (:8084 /loadpayload:) to SPAWN it
  -> pms-installer.elf, a fresh process, calls sceAppInstUtilInstallByPackage(uri),
     writes installer-res.json {ok, rc, content_id, token} and exits
  -> the console's own installer (BGFT) downloads the package from the PC - HTTP Range, resumable -
     and installs it, with the PS5's own toasts (Downloading -> Installing -> Ready to play)
  -> the PC shows progress from its own byte counter on /library/<key>, then waits for the proof below
```

Why a separate process: `sceAppInstUtilInstallByPackage` answers `0x80B2116F` when called from a
payload injected into a hijacked host (which the shop ELF is) and `0` from a freshly spawned one.
`pms-installer.elf` must be built with `-g`, never `-O2` (`build-installer-wsl.sh` does this).

**Proof.** An install is "playable" only when `bgft.db` (`tbl_downloads`) carries the title at
status **1036** (a base game) or **1026** (an update or add-on), and for a base game
`/user/app/<TID>/app.pkg` (or `/mnt/ext1/user/app/<TID>/app.pkg`) exists **with its bytes**. A row in
`app.db` proves nothing: it survives a reset and once produced 53 phantom installs. If the console
cannot be read, the job is neither passed nor failed on that basis - absence of evidence is not
failure.

**Where it lands.** A PKG goes wherever the console's own *Installation Location* setting points
(Settings > Storage). No API reports or sets that, and the app does not guess it; it only refuses
up front when a package fits on **no** connected drive. A **PS5 backup** is different: the console
copies the file into `/mnt/<drive>/homebrew` (or `/data/homebrew` for internal) on the drive you
pick, as `<name>.part` renamed only when complete, and ShadowMountPlus mounts it.

**Cheats and patches** are applied by the ELF itself to the running game (memory writes through the
kernel, with expected-bytes checks and revert). No third-party cheat engine is involved.

---

## 5. Updating

### 5.1 The ELF (the console)

```
python companion/deploy.py elf [path\to\PKG-MUTANT-SHOP.elf] [--ip <console>] [--expect 3.87.0]
```

It first checks that Payload Manager answers on `:8084` and that the shop is not in the middle of a
download, install hand-off or move (`--force` overrides), then uploads over FTP (2121, then 1337) to
the registered path as `.part`, verifies the size, keeps the old copy as
`/data/pkg-mutant-shop/PKG-MUTANT-SHOP.elf.prev`, renames, asks the running shop to quit, **waits for the shop
to acknowledge the quit and then for two consecutive failed connects to `:8710`** - on these
consoles a port that has stopped listening times out rather than refusing, so waiting for a refusal
would hang for ever, and one failed connect on its own can just be a busy accept loop - loads
through Payload Manager, and polls
`/api/health` until `version` is the expected one. By hand, the same four steps:

1. FTP the ELF over `/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf` (`.part`, then rename)
2. `GET http://<ps5>:8710/api/quit` and wait for `:8710` to close
3. `GET http://<ps5>:8084/loadpayload:/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf`
4. `GET http://<ps5>:8710/api/health` - check **both** `version` and `built` (the compile stamp).
   Same `built` as before = the old process is still running and owns the port.

Loading an older ELF rolls the UI back too, since the UI is rewritten from the ELF every boot.

### 5.2 From inside the app

Open **Payloads & Homebrews** in the top bar and use the **Updates** dropdown beside the close
button. It checks this project's own releases as well as every bundled payload's upstream, shows
what is newer, and updates them one at a time. A download is verified before anything is replaced:
the size must match, the marker must be present *inside* the bytes, and the file it replaces is
kept as `.bak` until the new one is proven complete. Taking an update replaces the file on the PC;
sending it to a console is a second, separate press.

### 5.3 The exe (the PC)

**It updates itself.** Open **Payloads & Homebrews -> Updates** and take the PKG MUTANT SHOP row:
the app replaces its own exe, starts the new build and closes the old one, once you have stopped
taking updates. `config.json`, `installed.json` and `.cache\` beside it are kept. Set
`updates.auto_restart` to `false` in `config.json` to be told to reopen it instead.

By hand, if you prefer: quit the tray app (right-click the tray icon - Quit), copy the new
`PKG-MUTANT-SHOP.exe` over the old one, start it. The copy silently does nothing while the app is
running - Windows will not replace a mapped image - so quit first and check the version afterwards.

### 5.4 Building

- exe: `build_exe.cmd` (runs `companion/PKG-MUTANT-SHOP.spec`, whose gates - version stamp, UI
  parse check, i18n, message style, storage-tile test - are all fatal). Output `companion/dist/`.
- PS5 ELF: in WSL, `bash ps5-app/onconsole/build-wsl.sh` (syntax pass, the installer first, the
  same gates, both bundles, then the link). Output `ps5-app/onconsole/PKG-MUTANT-SHOP.elf`.
- PS4 ELF: in WSL, `bash ps4-app/build-all-wsl.sh` - **the whole chain, not just the ELF.** The
  home-screen icon package carries its own copy of the payload, so building only the ELF leaves a
  stale icon that re-loads the previous build. This had no entry here at all.
- Toolchain: `bash ps5-app/payload/build-wsl.sh` once, fetches the ps5-payload-dev SDK and clang 18
  into `~/sdk` and `~/clang18` (no sudo).
- Toolchain, later: that fetch is guarded by `if [ ! -d ~/sdk/ps5-payload-sdk ]`, so it happens
  **once per machine and never again** - and the SDK is what decides which console firmwares the PS5
  build can run on. When a console is updated past it, the ELF cannot establish kernel read/write
  and dies before it logs anything: Payload Manager reports success and nothing starts.
  `bash ps5-app/update-sdk-wsl.sh --check` prints what yours knows against upstream; without
  `--check` it updates it and keeps the old one.

`python tools/ready_check.py --offline` checks the repo without a console;
`python tools/ready_check.py` waits for the console and checks the live lane.

---

## 6. Triage

| Symptom | Check | Meaning / what to do |
|---|---|---|
| Engine LED red, "not ready to install" | `http://<ps5>:8710/api/health` - `engine_ready` | `engine_ready` is "does Payload Manager answer on :8084". Reload Payload Manager on the console; nothing else is involved |
| Shop not answering (`http://<ps5>:8710` dead) | Payload Manager's page on `:8084` | the ELF is not loaded (it does not survive a reboot). Load it - section 2.2 - then check `version` in `/api/health` |
| Shop not answering, **and loading it changes nothing** | Payload Manager's `/log`, then `fw` in `/api/health` | it loads and no process appears: pldmgr logs `Sent <n> bytes to loader` and nothing starts, nothing reaches `install.log`, no toast. **The console's firmware is past what this build's SDK knows** - it cannot establish kernel read/write and dies before its first log line. `bash ps5-app/update-sdk-wsl.sh --check`, then rebuild. Beware: an OLD third-party payload fails the same way, so "somebody else's payload also will not start" does not mean the loader is broken |
| "Only packages already on the PS5 can be installed" | from the console's network, `http://<pc>:8710/api/health` | the console page found no PC. Start the exe; check the firewall on 8710; the PC announces itself every 8 s |
| Install refused, **0x80B21104**, no row in `bgft.db` | free space on the drive *Installation Location* points to | **out of space** - the installer refuses before it starts, so there is no download row. Free space or change the setting; the package is fine |
| **0x80B22404** | can the console reach `http://<pc>:8710/library/...`? | the console could not fetch the file: firewall, PC asleep, wrong network. Not an engine fault |
| **0x80B2116F** | `built` in `/api/health`; how the installer was built | the call came from an injected process, or `pms-installer.elf` was built `-O2`. Reload the shop from Payload Manager; rebuild the installer with `-g` |
| **0x80A4xxxx** | - | the console's own game database is sick. Rebuild the database from Safe Mode; the package is not the problem |
| New ELF loaded, old behaviour | `version` **and** `built` in `/api/health` | basename trap (section 2.2) or the old process still owning `:8710`. Overwrite the registered copy; quit and wait for the port to close |
| A game "installed" but its tile crashes | `app.pkg` under `/user/app/<TID>/` or `/mnt/ext1/user/app/<TID>/` | metadata-only registration. Delete the title on the console and install again |
| Backup mounts half-written / crashes the console | how it was uploaded | a file placed in a watch folder under its final name is mounted the instant it appears. Let the app copy it (it stages `.part`) |
| Blank page on every device, servers answer 200 | `python tools/check_web.py` | one bad token in `web/index.html`; both artifacts embed it |
| Console panics entering rest mode | Power > prepare for rest in the app (`/api/rest/prepare`) | it stops the helper payloads that do not survive suspend; the shop is reloaded by Payload Manager afterwards |
| Toasts never appear | - | the ELF sends the plain notification form only; the icon form returns 0 and draws nothing on this firmware. If a build stops toasting, that is what changed |

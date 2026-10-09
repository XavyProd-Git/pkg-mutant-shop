<div align="center">

# PKG MUTANT SHOP

**One app. Two jailbroken consoles. Every device in the house.**

A homebrew package manager and store front-end for a jailbroken **PlayStation 5** and
**PlayStation 4** — browse a library, see each game's updates, DLC, cheats and patches, and install
straight to the console: from the console itself, from a PC, or from a phone.

[![version](https://img.shields.io/badge/version-3.97.0-e8c547?style=flat-square)](CHANGELOG.md)
[![PS5](https://img.shields.io/badge/PS5-13.60-2a6fdb?style=flat-square)](#supported-firmware)
[![PS4](https://img.shields.io/badge/PS4-13.52-2a6fdb?style=flat-square)](#supported-firmware)
[![licence](https://img.shields.io/badge/licence-GPL--3.0-6aa84f?style=flat-square)](LICENSE)
[![languages](https://img.shields.io/badge/languages-15-9b59b6?style=flat-square)](#fifteen-languages)
[![YouTube](https://img.shields.io/badge/YouTube-%40XavyProd-ff0000?style=flat-square&logo=youtube&logoColor=white)](https://www.youtube.com/@XavyProd)

![The library](docs/images/library.png)

</div>

---

> ### Before you use it
>
> **Bring your own games.** This app installs what *you* point it at — your own discs, backed up by
> you, for your own console. It ships no catalogue, no links and no sources, and there is nothing in
> it that will go and find a game for you. That is a deliberate design decision, not an oversight.
>
> **Piracy is not what this is for.** XavyProd neither condones nor supports it. A tool that moves
> files cannot know where those files came from — that part is yours to get right, and it matters.
>
> **It is published for learning, for preservation, and for the fun of making it**, as-is and with
> no warranty of any kind. Jailbreaking your console, running homebrew and installing anything at
> all are decisions only you can make, and the consequences — to your console, your saves, your data
> or your account — are yours too. If that is not a trade you want, this is not the tool for you.
>
> Nobody involved in this project is responsible for what anyone does with it.

---

## What you get

|   |   |
|---|---|
| **Two consoles, one app** | A PS5 payload and a PS4 payload — one binary cannot run on both, which was measured, not assumed — behind one UI, one companion, one queue, one set of build gates. Both consoles get an icon on their home screen. |
| **Installs through Sony's own installer** | The console downloads each package *itself*, over HTTP Range, through BGFT. The PC serves bytes and keeps the queue — it never pushes a package into the console's installer. (One legacy path still pushes: a backup going to a console running an ELF too old to fetch for itself.) |
| **A verdict you can trust** | Never a guess. On the PS5 "installed" means a `bgft.db` status of **1036** (base) or **1026** (update/DLC) *and* a full-size `app.pkg` on disk. The PS4 keeps no such database, so there the proof is the title's own package *changing* — size and timestamp — which is the only honest test for an update, where the file was already there. A row in `app.db` alone is never proof on either. |
| **Cheats and patches, live** | Applied to the *running* game by our own engine — expect-gated and revertable. Thousands of cheat files ship inside the app. |
| **Payloads & Homebrews** | **The app is the sender.** Pick a payload and it goes to the console and starts, from any device — you do not go back to Payload Manager or GoldHEN by hand. Homebrew packages install through the same engine the games use, and both update from their own upstream GitHub releases — from a PC, or from the console itself with no PC on the network. |
| **Every device is one fleet** | Companions find each other on the LAN. A PC that does not hold a file can still press the button: whoever has the bytes is asked. |
| **Works with the PC switched off** | Every artifact carries the UI and the homebrew catalogue, and each console's payload carries the payloads it can start by itself. A console on its own also **downloads and updates for itself** — it reads what a project has released, takes the file that matches that console, checks it against the SHA-256 the project published, and installs it when it is a package. A console on its own is not a degraded mode. (Two things still want a companion: an emulator that ships a `.zip`, because neither console has anything to unpack one with, and the PS4's cheat library, which rides inside the PS5 payload and the Windows app.) |

---

## Screenshots

<table>
<tr>
<td width="50%"><img src="docs/images/game-panel.png" alt="A game's panel"><br><sub><b>A game's panel</b> — base game, updates, DLC, add-ons, and the mods that match this exact game version.</sub></td>
<td width="50%"><img src="docs/images/payloads-ps5.png" alt="Payloads and homebrews, PS5"><br><sub><b>Payloads &amp; Homebrews</b> — a tile is green only when a port answered or the loader listed the process.</sub></td>
</tr>
<tr>
<td width="50%"><img src="docs/images/payloads-ps4.png" alt="Payloads and homebrews, PS4"><br><sub><b>The same panel, PS4</b> — its own payloads, its own homebrews, taken from the folder layout.</sub></td>
<td width="50%"><img src="docs/images/library.png" alt="The library"><br><sub><b>The library</b> — every drive on both consoles, and every PC in the fleet, with free space.</sub></td>
</tr>
</table>

---

## Watch it

<div align="center">

[<img src="https://img.youtube.com/vi/Wa768pBs0j0/maxresdefault.jpg" alt="PKG MUTANT SHOP walkthrough" width="640">](https://youtu.be/Wa768pBs0j0)

**[PKG MUTANT SHOP 3.96.0 — walkthrough](https://youtu.be/Wa768pBs0j0)** · more on **[@XavyProd](https://www.youtube.com/@XavyProd)**

</div>

Recorded on 3.96.0, the build on the releases page right now — so what you see is what you get.

---

## What this is, and what it is not

**It is** a source-agnostic installer and manager. You configure your own content sources — a folder
on your PC served over HTTP, another PC running the same app, or your own self-hosted
`sources.json`. The app groups everything by game (base / update / DLC), shows install state, and
hands install jobs to the console.

**It is not** a scraper for any piracy site, and it ships **no bundled catalogue of copyrighted
games**. You point it at content you have the right to install. That keeps the tool clean, legal to
develop, and resilient — no dependency on some site's HTML staying the same.

**What it does ship besides its own code:** a cheat and patch library (json / shn / mc4 cheats and
patch XML, from the GPL-3.0 HEN-Cheats-Collection, credits inside each file), ShadowMountPlus, and
the helper payloads listed in the panel. Origins and licences are in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md).

---

## Supported firmware

| Console | Firmware | Needs |
|---|---|---|
| PlayStation 5 | up to **13.60** | Already jailbroken, with **Payload Manager** running on `:8084`. 67 firmwares are supported, all the way back to the launch build — the full list is below. |
| PlayStation 4 | 13.52 | Already jailbroken with **GoldHEN**. The home-screen icon then reloads the app without a PC. |

**Which jailbreak you use is not this app's business.** It does not install one, replace one or care
how you got there — it needs kernel access to already exist and Payload Manager (PS5) or GoldHEN
(PS4) to be running, because those are what load a payload. Any chain that gives you those works.

**The PS5 range is the payload SDK's, not ours.** The SDK carries the kernel offsets for each
firmware it knows; on one it does not know, the app cannot start **at all** — Payload Manager
reports success and nothing happens, with no message and nothing in any log. That is the single most
confusing failure this project has, so: each console reports its own firmware in the app, and you
are told when it changes.

<details>
<summary><b>Every PS5 firmware this build supports</b> (67)</summary>

```
1.00 1.01 1.02 1.05 1.10 1.11 1.12 1.13 1.14 2.00
2.20 2.25 2.26 2.30 2.50 2.70 3.00 3.10 3.20 3.21
4.00 4.02 4.03 4.50 4.51 5.00 5.02 5.10 5.50 6.00
6.02 6.50 7.00 7.01 7.20 7.40 7.60 7.61 8.00 8.20
8.40 8.60 9.00 9.05 9.20 9.40 9.60 10.00 10.01 10.20
10.40 10.60 11.00 11.20 11.40 11.60 12.00 12.02 12.20 12.40
12.60 12.70 13.00 13.20 13.40 13.42 13.60
```

Building it yourself? `bash ps5-app/update-sdk-wsl.sh --check` prints what your toolchain knows,
what is available upstream, and the difference. The list above is that output.

</details>

> **Updating the console is a one-way door.** A system update removes the jailbreak until one exists
> for that firmware, and even then this app has to be rebuilt against an SDK that knows it. The app
> never starts, resumes or cancels a console-owned transfer, and it can block Sony's update hosts if
> you point the console's DNS at the companion.

---

## Run it

You load the app **once per console, by hand**. After that the app does the loading — see
[Sending payloads](#sending-payloads-and-homebrews) below.

**On a PC** — run `PKG-MUTANT-SHOP.exe`. It creates `C:\Mutant Games\PS4` and `\PS5`, scans them,
finds the consoles on the LAN, opens <http://localhost:8710> and sits in the tray. `config.json` is
written beside the exe. Drop `.pkg` files into the folders.

**On the PS5** — with Payload Manager running, load `PKG-MUTANT-SHOP.elf` from it. The ELF writes
the UI, starts the helper payloads that are not already running (today ShadowMountPlus and
`ftpsrv`) once somebody has signed in, puts the **PKG MUTANT SHOP** tile under Media if it is
missing, and toasts that it is ready. Autostart can be delayed or switched off in the panel.

**On the PS4** — load `PKG-MUTANT-SHOP-PS4.elf` through GoldHEN, or press the home-screen icon. The
payload installs and updates that icon itself, from inside the ELF, with every PC switched off.
A PS4 payload does not survive a reboot and GoldHEN has no autoload folder, so **a running companion
hands it over again by itself** — after a restart the shop usually comes back with nobody pressing
anything.

**From source** — `python companion/server.py` (Python 3.8+, standard library only; Pillow optional,
for card thumbnails; pystray optional, for the system-tray icon), or `start.cmd`.

Ports, config keys, adding a second PC, triage: **[SETUP.md](SETUP.md)**.

---

## Using it from each device

The same page runs everywhere. Whichever device you open it on, the **console you are acting on** is
the one in the picker at the top — not the device you happen to be holding.

### On the PS5

Open **PKG MUTANT SHOP** under Media on the home screen. The console serves the page itself, so this
works with every PC switched off: your installed games, the backups on your drives, the cheats and
patches, and anything already on the console. When a PC companion is on the network the page finds it
and the PC's library appears too — the page re-points itself at whichever companion announced itself
most recently, so you do not configure an address.

The console's own browser has both a stick cursor and D-pad focus, and the page is built for both.

### On the PS4

Press the **PKG MUTANT SHOP** icon on the home screen. The icon re-loads the payload and opens the
page, so it is also how you bring the shop back after a restart without touching a PC. Everything the
PS5 page does is here, aimed at the PS4: its library, its drives, its payloads and homebrews, its
cheats.

Two differences worth knowing, both the console's and neither a bug: the PS4 keeps no `bgft.db`, so
"installed" is decided by the title's own data on disk; and the cheat library lives on the PC rather
than inside the PS4 payload, so cheats on that console want a companion running.

### On a PC

Run the exe. It is the only device that **holds files**: it scans your folders, serves packages to
the consoles over HTTP Range, keeps the install queue, and confirms every install against the
console's own records. It also finds other PCs running the same app — if this one does not have a
file, whoever does is asked for it.

More than one PC is normal. Each keeps its own folders and they fill in for each other.

### On a phone or tablet

Open the same address in any browser: **`http://<pc>:8710`** for a companion, or
**`http://<console>:8710`** for a console. The app prints the exact address under
**Settings → Devices & sources**, which is the easiest way to get it right.

There is nothing to install and no app store build — it is the same page, laid out for a narrow
screen: the controls fold into four rows, the filter and storage strips scroll sideways with a fade
to show there is more, and every control is a real touch target. A phone is a first-class way to
drive an install while the console is doing something else.

---

## Sending payloads and homebrews

**You load this app by hand once. After that, the app does the loading.**

Open **Payloads & Homebrews**, pick the console at the top, and press a tile. The app reads what that
console is already running, sends what is missing, and starts it — you do not go back to Payload
Manager or GoldHEN to load anything else.

| Press | What happens |
|---|---|
| **Send it** | The file goes to the console and starts. On the PS5 the app writes it into Payload Manager's own folder and asks Payload Manager to run it; on the PS4 it hands the bytes straight to GoldHEN's loader. |
| **Run** | It is already on the console, so nothing is copied — the console starts its own copy. The app checks first that the copy really is the one you are looking at. |
| **Install** | A homebrew package goes through the same install engine your games use, and gets the same verdict. |
| **Update** | The app checks each project's own GitHub releases and replaces the file in your folder. Taking an update and sending it are two separate presses, on purpose. |

A tile is green only when a port answered or the console's loader listed the process — never because
a file exists. Anything that changes the jailbreak layer says so and asks twice.

**The app does not replace your jailbreak.** Payload Manager and GoldHEN are still the loaders, and
they stay other people's work — the app drives them instead of making you do it. The one thing it
cannot do is load itself onto a console that has no shop yet, which is why the first load is manual
and a PS4 restart is handled by the icon or by a running companion.

Your payload and homebrew files live in `Mutant Payloads & HomeBrews` beside your games folders; the
app creates it, and each console's payload also carries the ones it can start on its own.

---

## Updating

The app checks its own releases and offers them **in the Payloads & Homebrews panel**, on the PKG
MUTANT SHOP tile, exactly the way it offers a new ftpsrv or nanoDNS. The download is verified before
anything is replaced: the size must match, the marker must be present *inside* the bytes, and the
old file is kept as `.bak` until the new one is proven complete. Taking an update replaces the file;
sending it to a console stays a second, separate press.

**The Windows app updates itself**, including the exe it is running from: it puts the new build in
place, starts it and closes the old one, once you have stopped taking updates. Set
`updates.auto_restart` to `false` in `config.json` to be told to reopen it instead.

Nothing needs configuring for this: the releases are public, so any device finds them. Not every
version is published — only the ones worth interrupting someone for.

---

## How installing actually works

1. **Install** is pressed on any device. The PC's queue claims the job — one running install per
   console.
2. The PC asks the console:
   `GET http://<console>:8710/api/engine/install-spawn?uri=http://<pc>:8710/library/<key>&name=…`
3. **On the PS5**, the ELF writes the request, writes its embedded `pms-installer.elf` into Payload
   Manager's own directory, and asks Payload Manager to **spawn** it.
4. `pms-installer.elf` — a *fresh process*, not injected code — calls
   `sceAppInstUtilInstallByPackage(uri)`, writes its verdict, and exits. From an injected payload
   the same call answers `0x80B2116F`; that is the whole reason for the separate process.
   **On the PS4** there is no spawn and no Payload Manager: the payload calls that console's own
   background-download service directly, from inside itself.
5. BGFT downloads the package from the PC with HTTP Range requests and installs it. The PC's byte
   counter on `/library/<key>` is the progress bar.
6. **Done** means, on the PS5, `bgft.db` 1036 / 1026 *and* a full-size `app.pkg` on disk. The PS4
   keeps no such database: there the console watches its own package change — size and timestamp —
   which is the only test that can tell an update apart from the file that was already on disk.

Where a PKG lands is the console's own *Installation Location* setting; the app does not guess it.
PS5 **backups** take a different lane: the console copies the file to the drive you pick — staged as
`.part`, because ShadowMountPlus mounts the instant a file appears — and then mounts it.

---

## Architecture

```
   any browser (PC · phone · the console's own browser via its home-screen icon)
                     │  http://<pc>:8710   or   http://<console>:8710
      ┌──────────────┴───────────────────────────────────────────────────┐
      │  web/index.html — ONE self-contained ES5 page, embedded in ALL    │
      │  THREE artifacts. Served by a console, it re-points its API at    │
      │  the newest PC companion that announced itself.                   │
      └──────────────┬───────────────────────────────┬───────────────────┘
                     │ /api/*                        │ /api/*
   ┌─────────────────▼───────────────┐   ┌───────────▼──────────────────────────────┐
   │ PKG-MUTANT-SHOP.exe   (PC)      │   │ PKG-MUTANT-SHOP.elf      (PS5, :8710)     │
   │ companion/server.py             │   │ PKG-MUTANT-SHOP-PS4.elf  (PS4, :8710)     │
   │ • scans C:\Mutant Games\PS4|PS5 │   │ ps5-app/onconsole/server.c                │
   │ • serves packages (HTTP Range)  │◄──┤ ps4-app/onconsole/server_ps4.c            │
   │ • install queue, one per console│   │ • serves the UI from its own disk         │
   │ • confirms via bgft.db          │   │ • spawns the installer per install         │
   │ • finds other PCs (federation)  │   │ • cheat / patch engine (both)              │
   │ • announces itself every 8 s    │   │ • home-screen icon, backups, payloads      │
   └─────────────────────────────────┘   └───────────────────────────────────────────┘
```

Deeper: **[ARCHITECTURE.md](ARCHITECTURE.md)**.

---

## Fifteen languages

English, Spanish, Portuguese, French, German, Japanese, Arabic (RTL), Chinese, Hindi, Russian,
Italian, Korean, Turkish, Polish, Dutch. Every key is covered in every language and a build gate
fails if one is not. One page for TV, PC and phone; controller-navigable, with real focus rings —
the console browser has both a stick cursor and D-pad focus, so the page uses `:focus`, not
`:focus-visible`.

---

## Building

| Artifact | Command | Notes |
|---|---|---|
| `PKG-MUTANT-SHOP.exe` | `build_exe.cmd` | Runs from `companion/`. Every gate is fatal. |
| `PKG-MUTANT-SHOP.elf` | `bash ps5-app/onconsole/build-wsl.sh` | PS5 payload SDK, in WSL. |
| `PKG-MUTANT-SHOP-PS4.elf` | `bash ps4-app/build-all-wsl.sh` | **The whole chain.** The home-screen icon carries its own copy of the payload, so building only the ELF leaves a stale icon behind. |

Deploy a new PS5 build with `python companion/deploy.py elf`. Payload Manager resolves a load **by
basename, from its own directory**, so the registered file is the one that has to change.

Each console build needs its own SDK in WSL; the build scripts say which and where they expect it.
The PS5 SDK is fetched **once**, so it stays on whatever was current the day the machine was set up
— and that is what decides which console firmwares the build can run on:

```bash
bash ps5-app/update-sdk-wsl.sh --check   # firmwares yours knows, what upstream has, the difference
bash ps5-app/update-sdk-wsl.sh           # update it, keeping the old one
```

---

## Documentation

| | |
|---|---|
| [docs/FEATURES.md](docs/FEATURES.md) | The full feature list, by area. |
| [SETUP.md](SETUP.md) | The current runbook: PC side, console side, triage. |
| [CHANGELOG.md](CHANGELOG.md) | Every version, in full. |
| [docs/release-notes/](docs/release-notes/) | What changed in each published release, and what to download. |
| [ARCHITECTURE.md](ARCHITECTURE.md) | How the pieces fit together. |
| [CONTRIBUTING.md](CONTRIBUTING.md) | Reporting something, and what a change has to pass. |
| [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) | Everything we ship that we did not write. |

---

## Licence and credits

**GPL-3.0** — see [LICENSE](LICENSE). Everything embedded that we did not write is credited in
[THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md), with its own licence.

The jailbreak, its loaders and the payloads in the panel are other people's work and stay theirs:
Payload Manager, ShadowMountPlus, GoldHEN, ftpsrv, nanoDNS, OnionHEN, the WebKit autoloader,
Itemzflow, FPKGi, RetroArch and PS4-Xplorer. This app replaces none of them, and the injection lane
on the PS4 is GoldHEN's, not ours.

Built by **XavyProd** — [YouTube](https://www.youtube.com/@XavyProd) · [walkthrough](https://youtu.be/Wa768pBs0j0)

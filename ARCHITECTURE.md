# Architecture

How PKG MUTANT SHOP is put together, as it is today (3.87.0). If something here disagrees with the
code, the code is right and this file is a bug.

---

## Three artifacts, one page

| Artifact | Runs on | Built by |
|---|---|---|
| `PKG-MUTANT-SHOP.exe` | Windows (the companion) | `build_exe.cmd` → `companion/PKG-MUTANT-SHOP.spec` |
| `PKG-MUTANT-SHOP.elf` | PS5, loaded by Payload Manager | `ps5-app/onconsole/build-wsl.sh` |
| `PKG-MUTANT-SHOP-PS4.elf` | PS4, loaded by GoldHEN | `ps4-app/build-all-wsl.sh` |

One binary cannot run on both consoles — that was measured, not assumed — so there are two payloads.
Everything above them is shared.

**`web/index.html` is one self-contained ES5 page with a single inline script, embedded in all
three artifacts.** There is no build step for the UI, no framework and no module loader: the page
has to run in two old console WebKits as well as a desktop browser, and the simplest way to
guarantee that is to ship one file that needs nothing.

```
   any browser  (PC · phone · the console's own browser, via its home-screen icon)
                     │  http://<pc>:8710        http://<console>:8710
      ┌──────────────┴───────────────────────────────────────────────────┐
      │  web/index.html — served by whoever you opened, then it points    │
      │  its API at the newest PC companion that announced itself         │
      └──────────────┬───────────────────────────────┬───────────────────┘
                     │ /api/*                        │ /api/*
   ┌─────────────────▼───────────────┐   ┌───────────▼──────────────────────────────┐
   │ the companion   (Python)        │   │ the console payloads   (C)                │
   │ companion/server.py             │   │ ps5-app/onconsole/server.c                │
   │ companion/payloads.py           │   │ ps4-app/onconsole/server_ps4.c            │
   │                                 │◄──┤                                           │
   │ • scans the games folders       │   │ • serves the UI from its own disk         │
   │ • serves packages (HTTP Range)  │   │ • spawns the installer, per install       │
   │ • one install queue per console │   │ • cheat / patch engine (PS5)              │
   │ • confirms installs from bgft.db│   │ • home-screen icon, backups, payloads     │
   │ • finds other companions        │   │ • answers with no PC present              │
   └─────────────────────────────────┘   └───────────────────────────────────────────┘
```

**The console serves the page; a PC serves the data.** Open the app on a console and the page comes
from that console, then re-points its API at a companion if one is on the network. This is why a
UI-facing fix usually belongs in `companion/server.py` — if it only lands in the console's C server,
the console never shows it.

Both console servers also answer on their own, with every PC switched off. That is not a degraded
mode: each ELF embeds the UI, the payload set, the catalogue and the cheat library.

---

## Installing

The console downloads each package **itself**, through Sony's own installer (BGFT), over HTTP Range.
The PC serves bytes and keeps the queue; it never pushes a game over FTP.

1. **Install** is pressed on any device. The companion's queue claims the job — one running install
   per console, claimed with a token rather than a flag so two devices cannot both win.
2. The companion asks the console to start it, handing over a URL on its own `/library/<key>`.
3. **PS5:** the ELF writes the request, writes its embedded `pms-installer.elf` into Payload
   Manager's directory, and asks Payload Manager to **spawn** it.
4. **PS5:** `pms-installer.elf` — a *fresh process*, not injected code — calls
   `sceAppInstUtilInstallByPackage` and exits. The same call answers `0x80B2116F` from an injected
   payload; that is the entire reason for a separate process.

   **PS4:** there is no spawn and no Payload Manager. The payload calls that console's own
   background-download service from inside itself, which is why steps 3 and 4 collapse into one
   there and why nothing on a PS4 needs a second process.
5. **Done** means, on the PS5, a `bgft.db` status of 1036 (base) or 1026 (update/DLC) *and* a
   full-size `app.pkg` on disk. The PS4 has no `bgft.db`, so there the title's own data on disk is
   the proof. A row in `app.db` alone is never treated as proof on either — it survives a database
   reset.

Where a package lands is the console's own *Installation Location* setting. The app does not guess
it and refuses an install only when the package fits on no drive at all.

PS5 **backups** take a different lane: the console copies the file to the drive you pick, staged as
`.part` because ShadowMountPlus mounts the instant a file appears, and then mounts it.

---

## The fleet

Companions find each other on the LAN and advertise what they hold. A device that does not have a
file is not a spectator: the console can start a payload it already carries, and anything else is
forwarded to the companion that has the bytes — which runs its own lane rather than having bytes
shipped to it.

Consoles are **tracked, not configured**. Each is followed by a durable id, so one that changes
address is still the same console, and no address is hardcoded anywhere.

---

## Cheats and patches

The PS5 ELF carries its own engine: it walks CR3 to reach the running game's memory, writes only
where the bytes it expects are present, and can put the originals back. A cheat is a **code patch,
not a value poke** — nothing changes until the patched instruction runs.

**The PS4 applies them too**, and this paragraph used to say it could not. The obstacle was real and
is still real from OUTSIDE a game: `mdbg` and `ptrace` both answer `EPERM` and GoldHEN's syscall
gateway is caller-only, all measured. The way past it is not to work from outside - the payload lists
a small plugin for the titles your library covers, GoldHEN loads it into the game at launch, and the
writing happens from inside the process that owns the memory. Same engine, same expect-gating, same
revert.

Two consequences worth knowing: the plugin is read by GoldHEN only at game START, so a title has to
be armed before it is launched; and the cheat library itself is not inside the PS4 payload - that
console takes it from a PC, which is why cheats there want a companion running.

---

## Ports

| Port | Who | Notes |
|---|---|---|
| 8710 | this app | the companion and both console payloads all use it |
| 8084 | Payload Manager (PS5) | the only external prerequisite on a PS5 |
| 9090 | GoldHEN's loader (PS4) | **never connect to it to probe** — opening and closing it stops it listening. Posting an ELF *is* the probe |
| 10101 | ShadowMountPlus | not 9021; 9021 is elfldr and is always up |
| 2121 / 1337 | FTP on the console | probe, do not assume |

---

## How this stays honest

Every build gate is fatal: the single inline UI script is parsed by Node before it can ship, all
fifteen languages are checked for every key the app uses, user-facing strings are checked against a
house style, the cheat library and payload catalogue are checked against their sources, and the exe
is checked for staleness. `python tools/ready_check.py` runs the lot.

Tests here are **perturbed to red** — when a check is added, the code it guards is deliberately
broken to prove the test fails. A check that passes when the code is broken is not a check.

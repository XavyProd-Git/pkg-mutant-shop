# Features

Everything the app does, by area. The numbers and the limits in here came off real hardware.

Legend: **PS5** · **PS4** · **PC** — where the feature lives. "Both" means both consoles.

---

## The library

* **Real metadata, real art.** Titles, versions, sizes and region come out of each package's
  `param.sfo`; the cards use the package's own `icon0.png`. Nothing is scraped and nothing is
  guessed from a filename. *(PC)*
* **Grouped by game, not by file.** Base game, updates, DLC and add-ons collapse into one title, so
  a game with six files is one card with six rows. *(all)*
* **Install state from the console**, read live. On the PS5 that is `bgft.db` plus a full-size
  `app.pkg` on disk; the PS4 keeps no `bgft.db`, so there the title's own data on disk decides. A row
  in `app.db` alone is never treated as proof on either — it survives a database reset and once
  produced 53 phantom "installed" titles. *(all)*
* **Every drive, with free space** — internal, extended storage, and each USB — for both consoles at
  once, plus every PC in the fleet. *(all)*
* **Search, filters and sort**: installed / not installed / has an update / add-ons, by platform, by
  name or size. *(all)*
* **Fifteen languages**, RTL included, with a build gate that fails when one language is missing a
  key the app uses. *(all)*

## Installing

* **The console installs it.** Every package goes through Sony's own installer (BGFT), which fetches
  the bytes from the PC over HTTP Range. The PC never pushes a game over FTP. *(both)*
* **The console fetches backups too, at the speed of the cable.** A backup is handed to the console
  as a URL on the PC that holds it, and the console pulls it with its own downloader — one hop, from
  whichever machine has the bytes. Measured on gigabit ethernet: **111.7 MB/s sustained (894
  Mbit/s)**, so a 35 GB game lands and mounts in a little over two minutes and a 112 GB one takes
  about seventeen. The PC pushes only for a game stored as a folder, or to a console whose payload
  is too old to be asked. *(both)*
* **A spawned process, not injected code.** `sceAppInstUtilInstallByPackage` answers `0x80B2116F`
  from a payload injected into a hijacked host, and succeeds from a freshly spawned one — so the app
  ships its own tiny installer and has Payload Manager spawn it per install. *(PS5)*
* **One install per console, claimed atomically.** The queue hands out a token, not a flag, so two
  devices pressing Install at the same moment cannot both win. *(PC)*
* **A verdict that is checked both ways.** "Console rejected this package" is not believed without a
  `bgft.db` row, and neither is "it worked" — the app has been wrong in both directions and now
  reads the databases that actually decide. *(all)*
* **Integrity before hand-off**, and an out-of-space refusal only when a package fits on *no* drive
  — where it lands is the console's own *Installation Location* setting and the app does not guess
  it. *(PC)*
* **Progress you can trust**: the byte counter on the file being served is the progress bar, because
  it is the same bytes the console is reading.

## Backups and drives *(PS5)*

* Copy a backup to **any** drive the console can see, staged as `.part` — ShadowMountPlus mounts the
  instant a file appears, and a half-written game crashes the console.
* Move and delete backups, with a two-tier root boundary: a drive root is not a watch folder, and
  confusing the two is how four separate bugs got in.
* **A large transfer is not cut off part-way.** Serving a package clears the send timeout the rest
  of the API runs with, because the console stops reading for as long as it takes to file away what
  it already has — routinely longer than thirty seconds on a big title.

## Cheats, mods and patches *(both consoles)*

* **Applied to the running game**, by the app's own engine — a CR3 walk to the game's memory, an
  expect-gated write, and a revert that puts the original bytes back.
* **A cheat is a code patch, not a value poke.** Nothing changes until the patched instruction runs,
  which is why the panel never claims an effect it cannot see.
* **Matched to the exact game version.** A cheat file built for 01.00 is not offered for 01.02; the
  panel has a version picker because the library genuinely differs between them.
* **Thousands of cheat and patch files ship inside the app**, so a console with no PC still has them.
* **The PS4 reaches the game from the inside.** A payload there cannot write another process's
  memory from outside — measured, not assumed: `mdbg` and `ptrace` both return `EPERM` and GoldHEN's
  syscall gateway only ever answers for its caller. So the app ships a small helper that is loaded
  *into* the game instead, and the payload lists it for exactly the installed titles your library
  has cheats for. The one limit that follows: the plugin list is read when a game **starts**, so a
  game that was already running when the helper was listed cannot be written to — start it again.

## Payloads & Homebrews *(both)*

* **Send a payload ELF to either console** — through Payload Manager on the PS5, through GoldHEN's
  loader on the PS4 — and **install homebrew packages** through the same engine the games use.
* **Identity is what is inside the file.** A payload is recognised by a marker string in its bytes,
  so renaming it changes nothing: it keeps its port, its upstream and its warnings.
* **Running is observed, never assumed.** A tile is green when a port answered, when the loader
  listed the process, or — for a UDP service like nanoDNS, which answers nothing from the LAN — when
  a bind test proves the port is taken. When nothing can be observed, the tile says so.
* **Upstream releases**, per item, with the asset that would actually replace *this* file — one
  project can ship both consoles in one release. The download is verified before anything is
  replaced, and the old file is kept as `.bak` until the new one is proven complete.
* **Test builds are found, and labelled.** Most of these projects publish their newest work as a
  pre-release, which GitHub's "latest release" deliberately hides — so an update you could see on
  the project's own page was invisible here. The panel reads the whole release list and marks a row
  as a pre-release, because taking a beta is your decision rather than the app's.
* **It says when it cannot tell.** An item the app could not form an opinion about used to be drawn
  nowhere at all, which on a panel whose only output is a count reads as "everything is current".
  Each one is now named with the reason — *the release has no file that could replace this one*,
  *this file does not say which version it is*, *the two are numbered differently* — and a link to
  go and look for yourself.
* **A jailbreak-layer payload is never started as a side effect.** Those need an explicit confirm,
  every time.
* **The app's own build is in this panel too**, and that is where its updates arrive. On a PC it
  fetches a new build in the background and then *offers* a restart rather than closing a window you
  are using — and a copy that did not unpack properly says so instead of quietly running with
  pieces missing. *(PC)*

## The fleet *(PC)*

* **Companions find each other on the LAN** and share their libraries; the console installs from
  whichever PC has the game.
* **Every device can press every button.** A PC that does not hold a file is not a spectator: the
  console can start a payload it carries itself, and anything else is forwarded to the companion
  that has the bytes — which then runs its *own* deploy lane rather than having bytes shipped to it.
* **Consoles are tracked, not configured.** Each one is followed by a durable id, so a console that
  changes address is still the same console, and nothing anywhere hardcodes an IP.

## On the console, with every PC switched off *(both)*

* Every artifact embeds the UI and the homebrew catalogue. What else each one carries differs, and
  the difference is what you can do with a PC switched off:
  | | UI | catalogue | payloads it can start | cheat library |
  |---|---|---|---|---|
  | `PKG-MUTANT-SHOP.elf` (PS5) | yes | yes | yes | yes |
  | `PKG-MUTANT-SHOP-PS4.elf` (PS4) | yes | yes | yes | **no** — taken from a PC |
  | `PKG-MUTANT-SHOP.exe` (PC) | yes | yes | the PS4's payload, to hand it over | yes |
* Each console serves the page itself, and the page then re-points its API at the newest companion
  that announced itself — so the same page is a full app with a PC and a working app without one.
* **A PC being switched off cannot slow the console down, let alone stop it.** Every outbound
  connection the console makes has a deadline — a host that is simply off does not answer, and
  waiting for the network stack to give up on it took over a minute each time. A companion that has
  gone quiet is not dialled at all, one that refuses is left alone for a while, and the whole search
  has a ceiling. The console draws its library immediately from its own storage and picks up a PC in
  the background if one appears.
* **A home-screen icon on both consoles.** The PS5 gets a tile; the PS4 gets a real application, and
  the payload installs and updates that icon itself, from inside the ELF, comparing versions and
  then bytes so a current icon is left alone.
* The PS4 helper **arms itself** for exactly the installed titles the library covers, because
  GoldHEN reads its plugin list only at game start.

## Housekeeping

* **Rest mode is safe.** The panic that used to follow it was other payloads, not this one; the app
  stands them down first.
* **PSN blocking** — point the console's DNS at the companion and Sony's update and telemetry hosts
  stop resolving. The app never starts, resumes or cancels a console-owned transfer.
* **Notifications that actually render.** The icon form of the PS5 toast returns success and draws
  nothing on this firmware, so the app sends the plain form.

## How it is kept honest

Every claim above is something the app can be held to, and the build refuses to produce an artifact
that breaks one: the UI script is parsed before it can ship, all fifteen languages are checked for
every string the app uses, and the cheat library and payload catalogue are checked against the
sources they are generated from. The engineering rules behind that are in
[CONTRIBUTING.md](../CONTRIBUTING.md); the measurements behind individual claims are in
[CHANGELOG.md](../CHANGELOG.md).

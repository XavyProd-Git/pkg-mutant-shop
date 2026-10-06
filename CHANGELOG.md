# Changelog — PKG MUTANT SHOP

All notable changes to this project are documented here.
Format follows [Keep a Changelog](https://keepachangelog.com/). Versioning is [SemVer](https://semver.org/).

Legend: `[VERIFIED]` = tested/confirmed · `[WIRED]` = implemented against a known spec, pending on-device test · `[STUB]` = placeholder with a clear integration point.

---

---

## [3.96.0] - 2026-10-02 - "Six times faster, and it finds the updates that were there all along" `[VERIFIED]`

### Fixed
- **A game on the PC you press Install from was PUSHED to the console, never pulled - and the fast
  lane was unreachable dead code.** `_run_mount` chooses between "the console fetches it itself" and
  "this PC pushes it" from `t["url"]` or `t["key"]`, and the job built for a local backup set
  **neither**, so the pull branch could not fire and every such transfer fell through to the push.
  The peer branch did set `url`, so pulling only ever happened for a game on a *different* PC.
  Measured from this console's own log: pushed **16.9-18.5 MB/s**, pulled **71-112 MB/s**.
  Marvel's Wolverine, 112 GB, was **1 h 50 m** pushed and is about **17 minutes** pulled. One line:
  the job now carries its key. The push stays as the fallback for a folder-shaped game and for a
  console too old to be asked.
- **Every bulk path on both consoles moved 64 KB per syscall.** Measured on hardware, writing 128 MB
  to the PS5's external drive and changing nothing else: **64 KB 42.7 MB/s, 1 MB 78.6 MB/s, 4 MB
  75.8 MB/s**. The whole cost was syscall count. 1 MB it is - 4 MB is worse and costs four times the
  memory. The PS4 asks for the same megabyte but falls back to 256 KB and then to its old 64 KB
  stack buffer if its heap refuses, and says in the log which it got.
- **Neither console asked for a socket buffer anywhere.** Set on the *listening* socket, where the
  window is negotiated during the handshake: **78.6 -> 108.7 MB/s**, which is the link and the same
  speed the console reads at. `/data` went **16.1 -> 74.3 MB/s**, so it was never the filesystem.
- **And none of that reached the path a game actually arrives through.** `http_download` creates its
  own outbound socket, which inherits nothing from the listener, and read at 256 KB. It gets the
  2 MB window and the megabyte buffer too - this is the socket an install pulls through.
- **Serving a package kept the accept loop's 30 s send timeout.** The install daemon stops reading
  for as long as it takes to promote what it already has, which on a large title is routinely longer
  than that, and a write that gave up part way through was an install that failed with nothing on
  screen to explain it. The PS4 has cleared it for some time; the PS5 does now.
- **PS4: a legal short write aborted a whole multi-GB upload.** `write()` may write fewer bytes than
  asked; this treated anything short as fatal and threw the transfer away. It uses `write_all` now -
  which matters more with a megabyte buffer, not less.
- **The Payloads panel could not see most updates, because GitHub's `/releases/latest` excludes
  pre-releases** and these projects ship pre-releases routinely. Measured: ShadowMountPlus had
  **1.7beta3** published while that endpoint still answered 1.7beta2 - which is exactly what the
  owner could see on the project's own page and the app could not. It reads the release list now and
  takes the newest published one, and **says on the row when it is a pre-release**, because a beta
  is the owner's decision to take.
- **`known-versions.json` was not in the exe.** `known_versions()` resolves next to `__file__`,
  which in a packaged build is inside the one-file extraction, so it answered `{}` - and the six
  payloads that write no version into their binary or filename came out of a live rescan blank. A
  blank version cannot be compared, so those six could never be offered an update by the only build
  anybody runs. Shipped and found when frozen.
- **A row the lane could not form an opinion about was drawn nowhere at all.** The panel renders only
  rows where `newer` is true, so "could not compare" looked exactly like "up to date". Every such
  row is now named, with what upstream actually has and a link to go and look: *no file that could
  replace this one*, *nothing attached to the release*, *this file does not say which version it
  is*, *numbered differently*.

### Added
- **`tools/check_console_net.py` now pins throughput as well as reachability** (30 checks, in
  `ready_check`): the buffers, the socket windows, the cleared send timeout, the PS4's allocation
  ladder and short-write handling, and that a local backup still carries the key that makes the pull
  lane reachable. Six perturbations on the new rules, each watched go red.

### Verified on hardware
- **Mortal Shell II, 35.1 GB, Casita -> PS5: 111.7 MB/s sustained (894 Mbit/s), mounted and
  playable in 132 s.** Deleted and re-installed to confirm it repeats: mounted again.
- **LEGO Batman, 40.6 GB: 111.8 MB/s, delivered byte-exact** (40,615,870,464) - ShadowMount did not
  mount that particular container, which is a separate matter from the transfer.

---

## [3.95.0] - 2026-10-02 - "The PS5 shop no longer needs a PC to be switched on" `[VERIFIED]`

### Fixed
- **A switched-off companion PC took the whole PS5 shop down.** `SO_RCVTIMEO` and `SO_SNDTIMEO`
  bound `read()` and `write()`, not `connect()`. Every outbound helper in the on-console server set
  both and read as though it were covered; none of them was. These headers are FreeBSD 11.4, where
  an unanswered SYN runs `net.inet.tcp.keepinit` - 75 seconds. `/api/library` merges each registered
  companion's library over HTTP, inline on the single accept loop, so one PC that was off froze the
  entire console API for 75 seconds at a time, re-armed every 20 seconds by the peer cache. The page
  could not be fetched at all (the browser leaves the bare URL in the title bar when that happens)
  and relaunching the ELF said "already running", because the port stayed bound throughout. Every
  `connect()` in the file now goes through `connect_deadline()`: non-blocking, `poll()` for
  writability, `SO_ERROR` for the verdict, 1500 ms. The PS4 was never affected - it gives every
  connection its own thread.
- **The dial loop was the one reader of the companion table that ignored staleness.** `PC_STALE_MS`
  is applied in five other places; the only path that actually opens a socket applied it nowhere, so
  a laptop last seen hours earlier was still dialled on every cache miss. It now skips anything
  quiet for `PC_DIAL_STALE_MS` (45 s - companions announce every 8), backs off a minute from one
  that refuses, and gives the whole merge a 2.5 s ceiling. **Measured on hardware with a dead PC
  registered: 1.55 s on the first dial, 0.047 s thereafter, and nothing at all once it goes quiet.**
- **A second, healthy PC could not rescue the console.** It had no chance to: the loop blocked on
  the dead entry before it ever reached the live one, and the browser gave up long before.
- **`/api/companion` offered PCs that had gone.** It is the list the page walks at four seconds per
  entry before falling back to the console's own API, and it was the only PC list in the file with
  no staleness filter. `g_pc_url` is no longer a last resort either - it carries no timestamp, so it
  put back the very address the filter had just dropped.
- **The console could remember `127.0.0.1` as its own LAN address, permanently.** `lan_ip_str()`
  memoised whatever it got on the FIRST call, including the loopback fallback it writes *before*
  attempting the route lookup. This payload is reloaded on every wake, so an early call during a
  cold boot pinned that value for the life of the process - and it is what `/api/health`,
  `/api/network` and `/api/consoles` publish as where to reach this console. Only a real address is
  kept now.
- **A remote stack overflow in the POST path.** `body` points into the 8 KB request buffer past the
  headers, and the guard compared `Content-Length` against the size of the whole buffer without ever
  subtracting that offset - so a POST with ordinary browser headers and a length just under 8191
  wrote past the end, from any machine on the LAN. Ported the PS4's existing fix, heap escape
  included. `413` above `POST_BODY_MAX`.
- **`/api/payloads` asked the payload manager what was running eight times, once per payload**, each
  with a 6 s read timeout on the accept loop. One fetch now, shared for 1.5 s, with a 2.5 s timeout.
- **A failed `accept()` was a bare `continue`** - a silent 100% spin with the port still bound,
  indistinguishable from outside from the stall above. It backs off and says so now.
- **PS4: `/api/sources` showed a green light for companions that had left**, the one reader of its
  table without the ten-minute rule its own neighbouring comment describes.
- **The page waited for every PC candidate before drawing anything.** Everything was inside
  `resolveApi`'s callback, so one switched-off laptop meant a blank shop while it was probed. The
  console's own API is already the one answering: it draws immediately and upgrades in the
  background if a PC appears - the pattern `retryCompanion()` has always used.
- **A remembered PC address was never forgotten.** `pms_pc` in `localStorage` was written on success
  and never removed, so a machine that had not existed for weeks was still probed on every load.
- **Failover took ~48 s, not the ~18 s its own rule intends.** The health poll inherited the 12 s
  default against a 6 s interval; it has its own 5 s timeout now.
- **A page pinned to one PC by `config.js` could never fall back.** `window.PMS_API` was final, so
  if that machine was off the page had no way back to the console serving it. It gets one chance,
  then comes home.

### Added
- **`/api/health` reports `conns` and `uptime_s` on the PS5.** "The port is open but nothing is
  being served" was a guess with no way to measure it - which is exactly the state this release is
  about. The PS4 has counted its connections for some time; this is the same number.
- **The console can serve its own UI from inside the ELF.** `WEB_FILES[]` is in memory for the life
  of the process and `serve_static` only ever read the copy on `/data`; a read that failed there
  answered 404 for the page itself with the port bound and every API route working - again
  indistinguishable, from a browser, from the stall above.
- **An honest "already running" notice.** A held port is not a working shop. It now asks the
  incumbent for its health first and says plainly when a copy is stuck, instead of sending the owner
  to open a page that cannot load.

### Guarded
- **`tools/check_console_net.py`** (20 checks, in `ready_check`): no raw `connect()` in the PS5
  server, the dial loop skips the quiet, both consoles' companion lists drop stale entries, the POST
  guard subtracts the body offset, the UI has an in-ELF fallback, and the PS4 keeps the
  per-connection thread that made it immune. Eight perturbations, each watched go red - **one of
  them passed at first**: the overflow check matched `(body - buf)` from a neighbouring line, so it
  now reads the `room =` line itself.

---

## [3.94.1] - 2026-10-01 - "What the restart left behind" `[VERIFIED]`

### Fixed
- **A self-update interrupted the outgoing build's cleanup.** The one-file exe unpacks ~56 MB into
  `%TEMP%` and deletes it on the way out; `_clear_replaced` force-killed it the moment the port came
  free, about three seconds after the handover, which is the middle of that delete. Two folders from
  one day prove it - 17 files and 13.6 MB left in each, identical, because an interrupted rmtree
  stops in the same place every time. It is now given twenty seconds to go by itself, and only
  forced if it has not (measured once at nine minutes, which is why forcing stays).
- **What it left could never be reclaimed.** `sweep_mei_leftovers` proves a folder is ours by the
  marker files inside it, and those were among the ones already deleted - so the debris was
  invisible to the only thing that could remove it. Measured on this PC: 169 folders, 88.9 MB. A
  build now writes down its own extraction path as it hands over, and its successor removes it by
  name. Nothing is inferred, so another program's folder can never be a candidate.
- **Both of the panel's Restart buttons work.** The app-incomplete row and the update-ready row can
  be on screen together and both offer the same control; as an `id` they were duplicates, and
  `querySelector` binds the first - leaving the lower one inert.

### Added
- **A copy that did not unpack completely says so.** `check_embedded()` runs before the first scan
  and reports it in the log, in `/api/health`, and in the Updates panel on every device, with a
  **Restart now** button - which `/api/app/restart` accepts in this one case even though the version
  is unchanged, because relaunching is the entire fix. Until now this was silent: `catalog()`
  returns `{}` for a missing file by design, so the curated layer simply vanished - every payload
  lost its repository, the Updates list had nothing to offer and the app could not see its own new
  version. It read as a broken update lane.

### Guarded
- The delete is gated on a **rename**, which Windows refuses while a file inside the folder is open,
  so a folder a live copy is running from is never touched - `rmtree(ignore_errors=True)` pointed at
  a folder in use is what produces a half-deleted one in the first place. Perturbed eleven ways
  across both files; each failure is caught by the check that names it.

---

## [3.94.0] - 2026-10-01 - "The app that could not see its own version" `[VERIFIED]`

### Fixed
- **The Updates list never asked what the APP was running.** `_payloads_updates` built every row
  from the catalogue item's `version` - the ELF sitting in the owner's folder - which is the right
  answer for every other payload and only half of ours. A PC whose folder was already current was
  told everything matched while the exe doing the telling was several releases old, and the only way
  out was copying the file across by hand or going to the releases page. That is exactly how the
  owner had been updating their second PC. Our row now carries `app_have` / `app_newer` as well, and
  is offered when EITHER half is behind.

### Added
- **`self_update_once()` and a background watch.** A packaged PC build checks its own repository on
  a slow clock (`updates.check_every_hours`, 6), installs a newer build with the existing
  `update_self()` swap, and records it. Nothing runs on a console: `running_exe()` answers None
  there, so there is no exe to replace and no download is attempted - which is also what makes
  "there is no companion" the no-op it should be rather than a special case.
- **`update_ready` on the wire** - in `/api/health`, `/api/payloads` and `/api/payloads/updates` -
  so a phone or a console learns that the PC it is talking to has a new build waiting.
- **`POST /api/app/update`** (fetch now) and **`POST /api/app/restart`** (take it). The restart
  route refuses unless a newer build is actually on disk; restarting into the same version is a
  pointless interruption.
- **The panel says so.** A row above the update list: *"Version X is ready"* with **Restart now**,
  in all fifteen languages.
- **A zip asset per release**, with all three artifacts, `SHA256SUMS.txt` and a read-me. The loose
  files stay - `pick_asset()` matches release assets BY NAME, so those names are a contract.

### Changed
- **`updates.auto_restart` now defaults to false.** 3.91.0 restarted the moment the new build was in
  place; closing a window somebody is using, mid-queue, because a release happened is not a thing to
  do unasked. The update still installs itself; only the moment is now the owner's. Set it true for
  the old behaviour.
- **README: the full supported firmware list** (67, from the launch build to 13.60, read out of the
  SDK) and **no named jailbreak** - the app needs Payload Manager or GoldHEN to be running and has
  no opinion about how kernel access was obtained. The old table said "Y2JB / Kstuff", which was
  true of 12.70 and is not what the owner's console runs now.

### Guarded
- `check_docs.py` refuses a PS5 firmware cell containing more than one version. `stamp_version`
  rewrites the FIRST version-looking token in that cell, so rewording it to a range silently turned
  "1.00 - 13.60" into "13.60 - 13.60" - which is precisely what happened while writing this entry.
  Perturbed with that exact string; caught.

---

## [3.93.0] - 2026-10-01 - "An item with no order jumps to the front" `[VERIFIED]`

### Fixed - the phone layout
- **Two of the header's eleven children had no `order`.** The header is a wrapping flexbox
  re-sequenced on a phone, and an item with no order defaults to 0 - which sorts BEFORE `order:1`.
  So `.qcol` (the install queue AND the Payloads & Homebrews button) and `#consoleSel` (the console
  picker) were drawn ABOVE the logo. Measured at 375px: a 319px header on an 812px screen, five
  ragged rows with the icon buttons interleaved between the status pill and the search field, and
  56% of the screen spent before the first game. Now 186px, four rows, 47%.
- **`.dock{order:3}` was a dead rule.** It named the install queue, which is a GRANDchild - it lives
  inside `.qcol` - and `order` only applies to flex items. It had done nothing since `.qcol` was
  introduced, while the comment above it described a row it could not produce.
- **The filter chips could not be scrolled at all**, so the last one was simply unreachable on a
  phone, and the drive strip hid 688px of its 1039px with no affordance. Both fade at the edge and
  snap now, and `edgeFade()` takes the fade away when there is nothing left - checked on scroll, on
  resize, and on a 1.5s tick because the strip is rebuilt whenever a console answers.
- **The queue pill wrapped inside itself** because its head needs 226px of content and the row gave
  it 193. The two recovery buttons (clear the install lane, clear finished) leave the pill on a
  phone - the first is documented by its own tooltip as something the app already does after every
  install - and at 320px the pill and the Payloads button stack rather than cram.
- **The panel header was three rows** with the close button stranded alone on the last one. Two now,
  title and close together.
- `tools/check_mobile.py` refuses a header child with no order, an order on something that is not a
  header child, and a scroller with no fade. Four perturbations, four caught. It parses the markup a
  TAG at a time - counting angle brackets per line found one child out of eleven, which would have
  reported OK for a header that was entirely unplaced.

### Fixed - a build break from earlier the same day
- Rewording the README's firmware cell to "up to **13.60**" left `tools/stamp_version.py` with no
  stamp point. It exits non-zero, and both console builds treat that as fatal. The pattern now
  matches the number whatever is written around it, and `check_docs.py` RUNS the stamper - checking
  that the values agree is not enough, because a reworded stamp point leaves every value correct.

### Changed - the documentation, against the code
Audited with 112 findings across eight documents; these are the ones that were verified against the
source rather than taken on trust.
- **The app sends payloads and homebrews itself**, and no public document said so - every one of
  them described loading by hand through Payload Manager or GoldHEN. New README section.
- **The README explains each device**: PS5, PS4, PC, phone.
- **PS5-only facts were stated as both consoles'**: the `bgft.db` 1036/1026 verdict (the PS4 has no
  `bgft.db` - 0 occurrences in its server), the spawn-an-installer step (the PS4 calls its own
  download service from inside itself), and "all three artifacts carry the cheat library" (only the
  PS5 ELF includes `cheat_bundle.h`; the PS4 takes the library from a PC).
- **ARCHITECTURE said the PS4 cannot apply cheats.** It can - `/api/cheat/apply` at
  `server_ps4.c:6426`. The obstacle it describes is real from outside a game and the way past it is
  a plugin loaded into the game at launch.
- **THIRD-PARTY-NOTICES contradicted itself**: "ships none of them" about Payload Manager and
  kstuff, sixty lines after listing both as embedded. `payload_bundle.h` settles it -
  `PB_INCBIN(pb_pldmgr, ...)` and `PB_INCBIN(pb_kstuff, ...)`. Both are redistributed, under their
  own licences.
- **CONTRIBUTING said `ready_check.py` is every gate** and nine suites were not in it. They are now
  (77 checks, up from 67); `test_fleet_two_consoles.py` stays out and is named, because it needs
  both consoles awake and skips itself otherwise.
- **SETUP** had no entry for the PS4 build at all, still said FW 12.70, still described replacing
  the exe by hand, and its triage had one cause for a dead shop. It now has the second: a console
  updated past the build's SDK, which looks like success and starts nothing.

---

## [3.92.1] - 2026-10-01 - "Recorded is not shown" `[VERIFIED]`

### Fixed
- `consoles_from_cfg()` rebuilds each console entry field by field, so the `fw` that
  `track_consoles` had just learned and saved was dropped on the way to `/api/consoles`. The number
  was in config.json and nowhere a person could read it, which is most of the point of having it.

---

## [3.92.0] - 2026-10-01 - "The console moved and the toolchain did not" `[VERIFIED]`

The PS5 app stopped starting entirely. It was not the build, the size, the disk, Payload Manager or
anything in this repository: **the console had been updated to firmware 13.60 and our SDK knew up to
13.40.**

### Fixed
- **The PS5 ELF is built against an SDK that knows 13.60.** The payload SDK carries a switch on
  `kernel_get_fw_version()` handing back the kernel offsets for each firmware it knows, and
  `-ENOSYS` for one it does not - so on an unknown firmware the ELF cannot establish kernel
  read/write and dies before `main()` reaches its own first log line. The symptom is silence:
  pldmgr logs a clean `Sent 45367592 bytes to loader`, no process appears, nothing is written to
  install.log, and no notification is drawn. Upstream added 13.42 on 2026-07-01 and 13.60 on
  2026-08-02; the installed SDK was from 2026-07-21 and stopped at 13.40.

  `ps5-app/payload/build-wsl.sh` fetches the SDK behind `if [ ! -d ~/sdk/ps5-payload-sdk ]`, so it
  is pinned to whatever was current on the day the machine was set up and nothing ever says
  otherwise. **`ps5-app/update-sdk-wsl.sh`** is new: `--check` prints the firmwares the installed
  toolchain knows, the ones upstream has, and the difference; without it the SDK is replaced and
  the previous one kept as `.bak-<stamp>`. It also rewrites `prospero-llvm-config`, which lives
  INSIDE the SDK directory and is destroyed by a plain re-extract - that failure surfaces much
  later as an unrelated linker error.

### Added
- **`/api/health` on the PS5 reports `fw`** (firmware is BCD: `0x13600000` is 13.60), and
  `track_consoles` keeps it beside the console id and **announces a change**: *"PS5 is on firmware
  13.60 now - it was 12.70. If its payloads stop starting, the build needs a newer SDK."* The
  console could have volunteered that number the whole time.

### What made this hard to see, recorded so it is not re-learned
- Every other payload on the console kept working, which reads as "so the console is fine". They
  were third-party builds that had been rebuilt for 13.60.
- Loading an OLD third-party payload (ps5debug-NG v1.3.2) failed exactly like ours - so the test
  "does somebody else's payload start?" answered "no" and pointed at a broken loader. It was not
  broken; that payload was simply built for an older firmware too. **A third-party payload is only
  a control if it is known to be current.**
- `/data` was writable, the size was irrelevant (a 118 KB ELF of ours failed the same way), and the
  previous release of our own ELF - which had run on that console two hours earlier, on the old
  firmware - also failed, which looks like "the console, not the build" and is true but incomplete.

---

## [3.91.2] - 2026-10-01 - "Restart when the pressing stops" `[VERIFIED]`

### Fixed
- **The restart no longer cuts "Update all" in half.** Our own entry is one row of that list, and
  restarting the instant it succeeded killed whatever was already in flight. Measured here: the PS5
  row reported success and restarted, and the PS4 row died with the connection. The restart is
  ARMED now (`schedule_relaunch`) and every further update request pushes it back
  (`defer_relaunch`), so the app goes when the panel has been quiet for eight seconds. Nothing was
  ever lost - the remaining updates were still offered - but the owner pressed one button and had
  to press it again, which is not what "it updates itself" should mean.
- **A self-update left two files beside the app.** `<exe>.old` is the build that was running and
  cannot be deleted at the moment of the swap - it is the image the process is executing from - so
  it is left for the next start. But that start waits only for the PORT to come free, and Windows
  can hold the image a moment longer, so the single attempt lost the race: measured, the app
  restarted itself correctly and a 38 MB `.old` stayed. A `.new` from a download interrupted by the
  restart stayed too, 25 MB that nothing reads. Both are swept, once inline and then in the
  background for a minute.

### Tests
- `tools/test_panel_rules.py` drives the arm/defer/fire sequence over real (short) timers with
  `relaunch_self` stubbed - it starts a second copy of the app and exits this one, which a test
  must never do - and checks three tiles asking still restarts exactly once. Three perturbations,
  three reds.

---

## [3.91.1] - 2026-10-01 - "Updating both tiles is one update" `[VERIFIED]`

### Fixed
- **update_self() is once per RELEASE, not once per tile.** Our own entry appears once per console,
  so "Update all" called it twice within a second. The first swap renamed the running image aside;
  the second found it there, could neither delete nor replace it - it is the file the process is
  executing from - and answered "Could not set the running app aside (PermissionError)" on a PC
  that had just updated itself perfectly. Seen live on the owner's second PC: the PS5 row reported
  success and the PS4 row, half a second later, reported that. The tag of the release that has
  been put in place is remembered, and relaunch_self() likewise starts one replacement however
  many tiles ask.

  The exe on disk cannot be read back to answer this instead - it is a PyInstaller archive whose
  copy of the page is compressed, which is why tools/release.py has to extract the bundle to read
  a version out of one.

### Tests
- The suite drives both calls and, for the second, puts a directory where `<exe>.old` goes: in the
  real thing that path is the running image, which the filesystem refuses to hand over, and a
  directory is the same refusal reproducibly. Perturbed: without the guard the suite reports the
  owner's exact sentence.
- Each download-integrity case now gets its own release tag. Sharing one made them skip the
  download and pass for the wrong reason once the guard existed - found by perturbing it.

---

## [3.91.0] - 2026-10-01 - "The panel believed one reply for ever" `[VERIFIED]`

Six defects behind two reports: a deleted app that still read **Installed**, and an update that
reported success and came back. Every one was measured on the owner's hardware before it was
touched, and each has a check that was perturbed to red.

### Fixed
- **A probe cache that could never expire.** `_payloads_list` re-stored its console probe with a
  fresh timestamp on every request - including the requests that only copied the previous values
  back out. The panel polls every three seconds and the entry expires after four, so looking at the
  panel kept it young for ever: the console was asked once, when the panel was opened, and never
  again. Measured: this PC answered `installed=True` for `PKGI13337` while the console's own
  `/api/library` did not list the title at all, and the owner's second PC - which nobody was
  watching, so its entry expired - answered correctly. The age of an answer is now the age of the
  answer, and the entry is only written back when something was actually re-read. It froze `live`,
  `running` and the console's reported version in the same way.
- **"Installed" counted titles the console does not have.** `installed_titles()` is every id the
  app list MENTIONS - right for the install-confirm loop waiting for a row to appear, wrong for a
  panel drawing the word next to a thing. The PS4 lists 21 titles of which 5 are system entries
  with no data on disk, and a deleted app keeps its row for a while. The panel now uses
  `installed_ids()`, which reads `install_status` - set on both platforms.
- **An update could download into the running exe's own temp directory.** `local_path()` answers
  with the copy bundled INSIDE the exe when the folder has none, and `download_asset()` took that
  as the place to write. It reported success, read the new version back out of the file it had just
  written, and the bytes went with the process. Measured on the owner's second PC: the update
  answered `{"ok":true,"version":"3.90.0"}` and the folder's fingerprint did not move - the same
  16-character hash before and after. PS4 only, because `bundled_ours()` matches no other file,
  which is exactly why the PS5 half of that tile updated and the PS4 half did not. The destination
  is now the owner's folder, always.
- **Size could not vouch for our own artifact.** Every "already on the console" shortcut compares
  lengths, and three consecutive releases of the PS4 payload are all 9,522,528 bytes - a version
  string of the same width does not move the length. So a freshly updated payload read as already
  there, the send was skipped, and the Run shortcut started the console's OLD copy while the panel
  showed the new number. `size_can_vouch()` narrows the shortcut to payloads that are not ours;
  theirs carry a version in the filename, so it still works for them.
- **A shipped catalogue entry described the machine that built the app.** It is generated by
  scanning the author's folder at build time, so its `version` is a release behind by construction
  and `version_from` said `"file"` about a file on another computer. That is why our PS4 tile
  offered "3.89.0 -> v3.90.0" on a PC already carrying 3.90.0 inside itself.
- **Three answers, not two.** `installed` arrives true, false or null, and null means the reply is
  not about that console. The tile collapsed null onto "Not installed" - asserting a fact nobody
  had checked - and a press on it installed over the top without the confirm row that exists for
  exactly that case.
- **The Updates list assumed instead of asking.** Taking an update deleted the row locally and
  zeroed the badge on the strength of the reply, so an update that changed nothing looked taken
  until a reload; and the list was frozen once filled, so one taken elsewhere kept its row.

### Added
- **The companion restarts itself after replacing its own exe** (`updates.auto_restart`, default
  on). 3.90.0 put the new build on disk and asked the owner to close and reopen the app - the one
  manual step left, and the one that does not get done. It now starts the new build and stands
  down; the new one waits for the port rather than racing the old one for it, because the
  single-instance guard would otherwise send it straight back out.

### Tests
- `tools/test_panel_rules.py` is new: it runs `installed_ids()` and `probe_cache_hit()` over
  simulated time, including "polling every second for 30s still re-reads the console about every
  4s, not once".
- `tools/test_payloads.py` drives `download_asset()` against a folder with no copy and a bundled
  copy elsewhere, and asserts the bytes land in the owner's folder and that nothing was written
  beside the bundled one. Eight perturbations, eight reds.

---

## [3.90.0] - 2026-10-01 - "The update lane could not update the app" `[VERIFIED]`

### Added
- **The companion replaces its own exe.** Payloads & Homebrews -> Updates could replace every
  payload and every homebrew and not the one file running it: `pick_asset()` only ever matches
  `.elf`, so taking the update on our own tile fetched the console's ELF, reported the new version,
  and left the PC running the old companion. Both consoles then read as up to date from a panel
  that was itself out of date - which is why "I updated it and it still said the old version"
  happened twice and was correct both times.

  Windows will not let a running `.exe` be overwritten and **will** let it be renamed, and that one
  fact is the whole method: download to `<exe>.new`, rename `<exe>` -> `<exe>.old` (allowed while
  running; the process keeps executing from it), rename `<exe>.new` -> `<exe>`, and delete the
  `.old` on the next start, when it is no longer anybody's running image. If the second rename
  fails the first is undone, so the worst outcome is the build that was already running, still
  running, under its own name. A short download and anything that is not a Windows program (an
  error page served in place of an asset) are refused before a rename happens. Nothing restarts
  itself; the toast asks for it and stays up long enough to be read.

  Taking the update from a build older than 3.90.0 still replaces only the console file, because
  the code that does this has to be in the build that is running.

### Fixed
- `live_catalog()` merged, not replaced - see 3.89.1. Carried here as the behaviour a second PC
  sees after it takes its first download.

### Tests
- `tools/test_payloads.py` drives the real swap over a `file://` release: both renames, the size
  check, the not-a-program check, the "nothing to replace when running from source" answer, and
  `running_exe()` naming no exe when the build is not frozen. All five were perturbed to red,
  including one that would have let the swap loose on the Python interpreter.

---

## [3.89.1] - 2026-10-01 - "One successful download emptied the shelf" `[VERIFIED]`

Reported from the second PC, and it is a regression this changelog has to own.

**Taking an update cleared the whole panel.** After updating, both tabs read *"Nothing here for this
console"* and no payload or homebrew was listed anywhere.

The chain, each step reasonable on its own:

1. A PC **without** the folder served the shipped catalogue, so all eighteen tiles appeared and
   could be pressed - that is the whole point of a second PC being part of the fleet.
2. Then the folder started being **created automatically** (3.87.0), so "no folder" stopped
   happening and an *empty* folder started. That was guarded: an empty scan falls back to the
   shipped catalogue.
3. Then the owner **took an update** on that PC. One file landed in the new folder. The scan was no
   longer empty, so the guard no longer fired - and the catalogue became that one file.

A single successful download emptied the shelf. Measured on the machine itself:
`source_here=True, items=1` - one PS5 payload, nothing else, which is exactly "nothing here for this
console" on the PS4 tab.

**An "or" between two catalogues was always the wrong shape.** A folder holding *some* of the items
is the ordinary case - it is what every PC looks like between the first download and the last. They
are merged now: the live entry wherever the folder has the file (its real version, size and hash,
which is what an update and a send need), and the shipped entry everywhere else, so the tile is
still there and `here` can honestly say the bytes are on the console or another PC.

Pinned by building exactly that folder - one file in a freshly created tree - and checking the panel
still describes the whole fleet, both consoles included. Reverting the merge reproduces the report
word for word: *1 item, PS5 only*.

### Also

The catalogue embedded in the artifacts was regenerated; our own entries move as the owner's folder
copies are updated, which is expected and is why the tile reads its version from the running build
rather than from the catalogue.
## [3.89.0] - 2026-10-01 - "One space in a folder name" `[VERIFIED]`

Every homebrew in the panel installs now, on both consoles. Only one of them ever did.

### The bug was a space in a URL

The console is handed a URL and gives it to its own installer, and **BGFT cannot fetch a URL with
spaces or brackets in it**. The PC was serving each homebrew under its path inside the owner's
folder, verbatim. From the PS4's own install log:

```
install: register failed rc=0x80991400 id=ED1633-PKGI13337_00-...
         uri=http://10.0.0.76:8710/library/PMS-HOMEBREW/Homebrews/PS4/PKGI PS4/FPKGi_v1.10.0-release.pkg
```

One space, in a folder the owner had named `PKGI PS4`. **Itemzflow installed perfectly from the
folder next to it because that one is called `Itemzflow`.** That is the entire difference between
the homebrew that worked and every one that did not - and it is why the owner's instinct was right:
*"itemzflow worked normally so it has the right install path, make fpkgi use the same."*

It caught FPKGi on both consoles, PS4-Xplorer (spaces **and** brackets) and the PS5's Internet
Browser - which this project had previously written off as *"the console's decision, let the console
decide"*. It was never the console's decision.

**Games never hit it** because the library renames them on disk: `normalise_pkg_names()` rewrites
any stem with characters outside `[A-Za-z0-9._-]`. Homebrews are deliberately **not** in
`library.local_paths`, because that renamer would rewrite the owner's own files - so nothing was
cleaning their names, and nothing should. The fix belongs on the way out, and that is where it is:
`serve_key()` is now built from the title id and the platform, which are stable, so a package that
is renamed or moved inside the folder keeps the same URL. It still ends in `.pkg`, because a URL
that does not is refused outright with `0x80990033`.

Percent-escaping is not a fix and was never an option - the PS5's own local lane measured that and
says so: *"It cannot fetch a percent-escaped URL ... the same request with a clean name returns
res:0, the escaped one 'install failed'."*

**Installed, on hardware, in this order:** FPKGi on the PS4, then PS4-Xplorer, then FPKGi on the
PS5, then the Internet Browser. All four reached "Ready to play". The PS4's log tells the whole
story in two lines - `register failed rc=0x80991400 ... /PKGI PS4/FPKGi...pkg` and then
`install: started task=338 ... /PS4-PKGI13337.pkg`.

### A folder app goes to the drive that mounts it

RetroArch is an app **folder** - 2,994 files, 268 MB - and the panel used to refuse it with "that is
a folder, not a package". The owner's instruction was to send it where ShadowMountPlus picks it up,
the same way the app already handles a game stored unpacked.

It turned out that lane was already complete: `_install()` routes on what the registered path **is**
(a directory goes to the mount lane, a file to the package lane), the recursive folder push has been
in the queue worker all along, and the console needs no new route. The only thing missing was that
folder-shaped homebrews were never **registered**, so the lane could not find them. They are now, and
a press sends RetroArch to `/mnt/ext1/homebrew` and ShadowMountPlus mounts it. Verified: **"Mounted -
ready to play"**, and the folder is on the drive.

**PS5 only**, as instructed - ShadowMountPlus is a PS5 payload, and a PS4 asked for a folder app is
told why rather than being sent 268 MB it cannot use.

### A mounted title is installed

`title_has_data()` required the title's own `app.pkg` with bytes. A mounted folder app has none and
never will - nothing was installed, the container is mounted in place - so RetroArch sat on the home
screen while the panel called it "not installed". `mount.lnk` is what ShadowMount leaves for exactly
this, and this server already looks for it elsewhere; now the installed check does too.

### Two things found while proving the rest

**The PS5's reply was silently truncated.** `/api/payloads` built its JSON in `out[2600]` while its
parts - live, have, apps, have_p and the boot log - total about 6.7 KB. Adding one field pushed it
over, and the console answered exactly 2599 bytes of invalid JSON. snprintf does not complain. The
buffer is sized from its parts now and says so if it is ever short, and a check compares the two.
The same shape of failure once cut a 117-entry title list off at 72.

**A gate could not fail, for the second time in this project's history, for the same reason.** A
regex in the test suite had been written as a word boundary and reached the file as a literal
**backspace byte**: it matched nothing, so the check passed whatever the code did - which is how it
was found, by perturbing the code and watching it stay green. A terminal prints 0x08 by moving the
cursor back, so the broken line renders as though it were correct.

`tools/check_control_chars.py` now refuses any tracked text file carrying a control byte that is not
a tab, newline or carriage return - 4,331 files, and it was perturbed to red by putting the
backspace back.

### Gates

`tools/test_payloads.py` is at **298 checks**. The serve key is checked against every catalogue entry
for anything a console installer cannot fetch, and for uniqueness; the folder lane is pinned at the
registry, the refusal and the lane choice. Every new check was perturbed to red - and three that
first passed while the fix was reverted were rewritten until they could fail.
## [3.88.1] - 2026-10-01 - "The PS4's install lane was reporting itself busy for ever" `[VERIFIED]`

Found while testing 3.88.0 on the consoles, by trying the install the owner had reported.

**A finished job is not a running one.** `active` stays set after a job ends so the panel can still
show how it went, and the gate that accepts a new install already knew that - it tests `active` AND
a non-terminal state. `/api/engine/state` did not, so once the PS4 payload installed its own
home-screen icon at boot - which it does every time it loads - the engine reported **busy** for
ever. The install lane was free the whole time; it just said it was not.

**And a state nobody refreshes never ends.** Correcting the busy test alone was not enough:
`/api/engine/state` never called `job_refresh()`, which is the thing that turns a finished transfer
into `installed` or `error`, so it was reading whatever the job last looked like. `/api/engine/job`
has always refreshed first - two routes describing the same job must not disagree. Verified on
device: `state=ready busy=False` after the boot task, where it had been `busy` indefinitely.

**`dry_run` was not forwarded, so asking what an install WOULD do performed one.**
`/api/install` grew that flag after a test suite installed real games on the owner's console.
`/api/payloads/install` builds its own body for that lane and was dropping the flag - the same trap,
one layer up. Caught by using it: a "dry run" of FPKGi queued a real attempt.

### What was checked on hardware

* **Every payload**: all seven third-party payloads on the PS5 and both on the PS4 are present on
  the console and can be started with no PC on the network.
* **Every homebrew**: each one's install was asked (for real, with `dry_run`) on the console it
  belongs to. The only refusal is RetroArch, which is correct and already explained - it is an app
  **folder**, not a package, so it goes on a drive the console mounts.
* **FPKGi is refused by both consoles.** The package is intact - the companion's own integrity check
  reads it as *"complete (pfs image fits + tail high-entropy)"* and the file matches the catalogue
  byte for byte - and the engine was free when it was retried. Both consoles simply decline it. That
  is the console's decision to make, and the app now reports it as an error instead of the "Done" it
  used to claim.
## [3.88.0] - 2026-10-01 - "Installed means the game is there" `[VERIFIED]`

Three things the panel reported that were not true, all found from one session on the consoles.

### A title was called installed because its artwork folder existed

`app_ids_json()` on both consoles listed the directories under `/user/appmeta`. **appmeta is
artwork.** It is written early in an install, it is left behind by one that failed, and it survives
a database reset - which is how 53 titles were once reported installed on a console holding none of
them. Pressing Install on a homebrew the console did not have therefore answered "Done" and then
showed it as **Installed**, with nothing downloaded and nothing installed.

Both consoles now use the proof the rest of the code already trusts, and that the install lane
already waits for: **the title's own `app.pkg`, with bytes**, under a root a game can live on. The
PS4 even had the helper for it (`installed_app_pkg()`, *"app.pkg WITH BYTES is the only honest
proof"*) and was not using it.

Measured on the owner's consoles the moment it shipped: **PS5 117 -> 88 titles, PS4 17 -> 15**.
Twenty-nine of those PS5 entries were folders with no game behind them.

### "Done" was said before a byte moved

An install is handed to the queue, which answers `{ok:true, ids:[...]}` with no message of its own -
so the panel fell through to its default and said **Done** the instant the job was *accepted*. The
queue is what knows how an install ended; the press now says it was queued and where to watch it.

### Our own tile could never show the right version

The catalogue is generated from the owner's folder at **build** time, so the copy baked into an ELF
records what that folder held when the ELF was built: one release behind, for ever. A console
running 3.87.0 said **"Running - 3.86.0"** and was offered an update it already had.

A running program is the only thing that knows its own version, so both consoles now report theirs
(`shop_version`), the companion passes it through, and the tile uses it whenever the shop is
running. Verified on device: the tile reads **Running - 3.88.0**.

While there: the PS4's `/api/payloads` was missing `bundled` and `status`, which the PS5's has had
all along, so anything reading them could not tell "this console carries none" from "this console
did not say".

### A second PC refused every update it offered

`live_catalog()` falls back to the shipped catalogue when the folder is empty, so a companion with
no folder lists all eighteen items and offers updates for them - then answered **"That file is not
on this PC, so there is nothing to replace"** for every one. That is true and useless: the app knows
the project, the release, the asset, and exactly where the file belongs.

Not having a file is now a reason to fetch it. The destination comes from the catalogue, the folder
is created if it is missing, and the backup step is skipped when there is nothing to back up.

### A console with no companion could not check for updates - and said everything was fine

Neither console implements `/api/payloads/updates`, and an unknown route answers **HTTP 200 `{}`**
on purpose so the UI does not error. The panel read that as an empty list and reported *"everything
matches the newest release"* - a confident false negative, the one answer a check must never give.

A reply with no `items` is now told apart from `items: []`. And when there is no companion to ask,
**the page asks GitHub itself**: neither console can speak HTTPS (no TLS stack in a payload, and the
PS5's downloader refuses any url that is not `http://`), but the page runs in the console's own
browser, which has one. It checks this project's release and compares it against the build that is
answering. Taking the download still needs a companion, and the row says so with a link rather than
a button that cannot finish.

### The companion had been asking a dead address for the PS4

Found by a build gate, not by looking: *"the PS4 payload is the same version too - None vs 3.88.0"*
while the PS4 was plainly answering. The companion had it saved at `10.0.0.90`; it was at
`10.0.0.86`.

Addresses are supposed to self-heal by the console's durable id. This one could not: the PS4 had
**also regenerated its id**, so the saved pair matched nothing, the id-first lookup found no
candidate, and the platform fallback was skipped - because it only ran for an entry with **no** id,
and this entry had one. An id nothing on the network reports identifies nothing, which makes it
exactly as useless as having none, and it failed the same way: permanently.

That one stale address is why every PS4 tile in the panel read as unreachable from the PC, and why
switching to the PS4 tab kept showing the PS5's payloads. After the fix the companion found it on
its next pass, with no input: `ps4 ip=10.0.0.86 online=True`.

A second console of the same platform is still refused - the fallback fires only when exactly one
console of that platform is unaccounted for, and it announces the move.

### Also

* The published binaries carried the build account's home directory in their DWARF paths.
  `-ffile-prefix-map` rewrites recorded paths only - no effect on codegen, so *-g, never -O2* still
  holds. All three artifacts verified clean.
* `tools/test_payloads.py` is at **272 checks**. Every new one was perturbed to red, and two that
  survived their first perturbation were rewritten: one passed because a *comment* named the
  function it was meant to be testing.
## [Unreleased] - documentation

A pass over every document in the repository, splitting what a reader needs from what we wrote for
ourselves. No code behaviour changed; the three artifacts are untouched.

**Twelve documents left the repository and stayed on disk under `internal/`.** Five of them opened
with their own **SUPERSEDED** banner - a public document whose first paragraph lists "what is no
longer true" teaches a stranger nothing except that our docs contradict each other. The rest are
working material: the install engine's outage runbook, the release workflow, two roadmaps, the
payload panel's in-progress design record, and the whole `research/` folder.

**`research/` also held a partial copy of etaHEN's source, and `ps5-app/onconsole/payloads/`
held its binary.** Neither is embedded, started or spoken to - the binary had been dead weight
since 3.33.0 - and redistributing somebody else's GPL work from a public repository carries an
obligation there was no reason to take on for a file nothing uses. Both are out; the notice in
`THIRD-PARTY-NOTICES.md` now says so plainly.

**Things that should never have been published:**

* **two PSN user ids**, in plain text in `ps4-app/onconsole/README.md`;
* **the owner's Windows account name**, twice in `CHANGELOG.md`, once as a home path and once as a
  machine name in a diagnostic dump;
* a pointer at the git history telling readers where a device id could be found;
* the owner's habits - a console used while they were asleep, and why one installed title is on the
  cheat skip list. The skip list stays and still explains itself; it no longer names the game or
  the reason.

**And the way the work is done is not a release note.** Eleven entries described audits by how many
readers ran them, one reported that a reviewer had crashed, and another that a usage limit cut
verification short. What an audit *found* is worth publishing - the counts of confirmed and refuted
findings are kept - but the machinery that produced it is ours. Nineteen first-person lines
("What I shipped that was wrong", "My fault, and it came from 3.4.0") now read in the project's
voice; three remain because they are quotations.

### Corrections, found while reading

* **`docs/FEATURES.md` said PS4 cheats could not be applied. They can.** The PS4 ELF embeds
  `pms-agent.prx` (`agent_bundle.h`, included at `server_ps4.c:82`, six matches in the built ELF)
  and lists it for exactly the installed titles the library covers. What is true is narrower and is
  now stated: a payload there cannot write another process's memory *from outside*, so the helper
  is loaded into the game instead - and because the plugin list is read when a game **starts**, a
  game already running when the helper was listed cannot be written to.
* **`SETUP.md` said a deploy waits until a connect to `:8710` is refused.** It does not, and could
  not: on these consoles a port that has stopped listening **times out**, so waiting for a refusal
  hangs for ever. `deploy.py` waits for the shop to acknowledge the quit and then for two
  consecutive failed connects.
* **`SETUP.md` said only ShadowMountPlus auto-starts.** ftpsrv does too (`payload_bundle.h`); six
  of the eight are written and deliberately left alone.
* `SETUP.md` introduced itself as 3.62.0 with "Two artifacts, nothing else" twenty lines above a
  section naming all three.
* `THIRD-PARTY-NOTICES.md` said "two shipped artifacts" and was stamped 3.84.0.

**`ARCHITECTURE.md` is new.** The old one was a July design-and-vision note that had been
superseded since September; it is in `internal/`. The replacement describes the system that exists.
**`CONTRIBUTING.md` is new** and carries the engineering rules that used to be scattered through
documents nobody outside this repo could read: perturb a test to red, measure rather than assume,
never guess a syscall signature, and the two things the console browsers do differently.

## [3.87.0] - 2026-09-30 - "The app makes the folder, and the updates move into the header" `[VERIFIED]`

### The official repository

**[XavyProd-Git/pkg-mutant-shop](https://github.com/XavyProd-Git/pkg-mutant-shop)**, public, is the home of
this project. Every reference points there - `assets/payloads/curated.json`, the generated catalogue
that both ELFs and the exe embed, the issue-template links, `README.md` and `RELEASING.md`. The slug
lives in the curated table and flows into the generated catalogue, so changing it means re-running
`tools/gen_payload_catalog.py` and rebuilding all three artifacts.

### A missing folder is something to make, not something to announce

The panel used to print **"That folder is not on this PC: C:\Mutant Payloads & HomeBrews"** across
the top and leave the owner to go and create it - on a machine that knew the path, knew the layout,
and had just decided it could do nothing without it. The games side had had a *Create folders*
button in Settings since early on, which is the same job with the same knowledge behind it; the only
thing a button added was a chance not to press it.

* `ensure_tree()` and `ensure_source_tree()` make `Payloads/PS4`, `Payloads/PS5`, `Homebrews/PS4`
  and `Homebrews/PS5`. The layout is **derived from what `scan()` walks**, so the folders that get
  created and the folders that get read cannot drift apart - and the test derives one from the other
  rather than listing either by hand.
* `ensure_all_folders()` runs at startup over **every** configured library root and the payloads
  root, before the first scan. The *Create folders* button stays: pressing it on a specific root is
  still a sensible thing to want.
* Failures are values, never exceptions - a drive that is not plugged in must not stop the app
  booting.
* **The two trees stay separate, deliberately.** The payloads root is still not in
  `library.local_paths`: `Library.scan()` registers every `.pkg` it finds as a game and
  `normalise_pkg_names()` **renames files on disk**, which would rewrite the owner's homebrew
  filenames and put five homebrews on their game shelf.

**And the regression that creating it introduced, caught before it shipped.** A PC with no folder
used to fall into the "not a directory" branch and serve the *baked* catalogue - which is what puts
all eighteen tiles in front of a second PC that holds none of the files, sourced from the console or
a peer. Create the folder and that branch stops being taken: the scan succeeds, finds nothing, and
the panel goes blank. An empty scan now falls back to the baked catalogue, with the live signature,
so the first file dropped in is still noticed on the next poll.

### Updates moved into the panel's header, as a dropdown

The header row reads **Back · PS4/PS5 · title · Updates · close**, the owner's order. The updates
band that used to sit across the top of the panel body is now a pill beside the close button: it
carries the count, so there is something to see without opening anything, and its drawer floats, so
opening it never reflows the grid behind it. Inside: every item with its own button, **Update all**,
and **Check for updates**.

*Update all* runs them **one at a time and stops at the first refusal**. Six parallel downloads over
one connection is how a 45 MB payload and a 9 MB one both arrive truncated, and a failure halfway
through a parallel run leaves nobody able to say which files were replaced.

**Three traps this file already knew about, paid attention to rather than rediscovered:**

* `.sheet` is a scroll container, so an absolutely positioned drawer inside it is **clipped** -
  measured on the PS4 tab at **310px of a nine-row drawer simply gone**. It uses the `placeMenu()`
  helper this file already had for exactly this, which measures, clamps to the viewport, and
  re-parents to `<body>` first because any transform on an ancestor breaks `position:fixed`. The
  consequence is written down: once open the drawer is **not inside `#phb`**, so none of its CSS may
  be scoped there and closing the panel has to close the drawer explicitly.
* A drawer that is merely transparent **keeps its buttons in the D-pad focus order** - here that
  would be an Update button pressed with no row on screen to say what it was replacing. Verified
  live: **0 of 5 buttons focusable** once closed.
* `:focus`, not `:focus-visible` - the console moves focus with the D-pad and never reports the
  heuristic `:focus-visible` waits for.

### "Our own releases are private for now" is gone

Along with the key behind it, in all fifteen languages. A check that cannot see a release is now
counted with everything else that could not be checked, which is all that can honestly be said
about it.

### Gates

`test_payloads.py` is at **255 checks**. Seven new ones cover the folder tree, the empty-folder
fallback, the header order, the fixed drawer, the panel-close coupling and both removed messages -
each perturbed to red before being kept. A note for next time, learned here: **`check_web.py` cannot
catch a deleted element id.** It proves the script parses; `$("#gone").onclick = ...` parses
perfectly and throws at load, blanking the app on every device while the servers still answer 200.
The panel was opened, driven and closed in a real browser with an error listener attached - zero
errors - and that is the check that mattered.

## [3.86.0] - 2026-09-30 - "Our own GitHub, and three bugs that were wearing federation's coat" `[VERIFIED]`

The owner asked why the two PCs were not pairing. They were - in both directions, the whole time.
Three other things were wrong, and each of them produced a symptom that looked exactly like a
federation fault. Then the app got its own repository, and its own tile became the place its
updates arrive.

### "Federation isn't pairing" - it was, and here is what was actually broken

**`/api/federation/peers` reported a config field that gates nothing.** `federation.enabled`
defaults to `false`, auto-discovery has never consulted it, and `federation.peers` is only a list of
hand-written extras - so a fully paired machine cheerfully reported `enabled: false`. That is what
sent us looking for a pairing fault. It now answers the question it appears to answer: *are we
federated* - plus `discovery` and how many peers were configured by hand.

**A PC said "Not on this PC" about a file it was carrying inside itself.** The exe has bundled
`ps4-elf/PKG-MUTANT-SHOP-PS4.elf` since long before this panel existed, but `local_path()` only
ever looked in the owner's payload folder. On a machine without that folder the PS4 shop tile was
greyed out and unpressable while the bytes sat in the running process. `bundled_ours()` finds them.
Frozen builds only: from source the repo copy is found the ordinary way, and a checkout must not
claim a build it has not made.

**The PS5's ELF is 34 MB and deliberately not bundled** - so a second PC can only ever get it from
the PC that built it, which needs us to *offer* it. `fleet_summary()` skipped our own artifacts on
the reasoning that every machine builds its own; that is not the situation anyone is actually in.
It advertises them now. The peer runs its own deploy lane rather than having bytes shipped to it.

**And size is not part of that match for our own artifact**, which is the whole question being
asked. For a third-party payload it is *"is this the same build the tile is describing?"* - a peer
holding a different ftpsrv is not a substitute for the one the owner chose. For our app it is *"can
anyone here give me the shop at all?"*, and any build is an answer. Measured, not supposed: the
second PC's baked catalogue recorded our PS5 ELF at 34,139,632 bytes - the copy that was in the
folder when its exe was built - while the PC beside it advertised the 45,350,920-byte build, and the
sizes disagreeing made the tile say "nobody has this" about a file on the same LAN.

### The tile that said "Running" and did not look it

The tile's colour was chosen by testing "is the file here?" **before** "is it running?", so a
payload the console was plainly running was painted as unreachable. On a second PC that is exactly
our own shop entry: nothing on that PC, nothing in the bundle to send, and unmistakably alive on the
console in front of you. **The colour follows the sentence now** - running wins, and where the file
is only decides the colour when nothing is running.

### Our own GitHub, and the app's own tile as the update lane

There is a repository now, and the app reads its releases the same way it reads ftpsrv's:

* our catalogue entry carries a `repo`, an `asset`, a `marker` and a version pattern like any other
  upstream, so the existing per-item update row offers a new shop build, verifies the marker
  **inside** the download, writes `.part` and only then moves, and keeps the old file as `.bak`;
* `pick_asset` already resolved PS4 vs PS5 out of one release, which is why this cost almost no new
  code. Three guards had to go, each correct when it was written: the update check skipped `ours`,
  the update action answered *"This app updates itself, not from here"*, and the fleet did not
  advertise `ours`;
* **our version can never be read by `identify_elf`, and the reason is structural.** The PS5 ELF
  embeds ftpsrv, nanodns, kstuff, OnionHEN, Payload Manager and the WebKit autoloader, so every one
  of those projects' markers is inside it; the identifier requires exactly one hit and would answer
  "unknown" for ever. That is why our own entry showed no version at all. It is read from the one
  line only our own build writes, `var APP_VERSION="…"`, and it immediately proved its worth: the
  copies in the owner's folder are **3.83.3** while the console runs 3.85.0;
* **the tile now shows the version of the file, not of the page you are reading.** It used to show
  the running build, which sounds right and is wrong - the button sends the *file*, and those are
  routinely different. Showing the running number would have had the update row offer
  "3.85.0 → 3.86.0" about a file that was two builds older, and a press would have sent the old one.

**A private repository's 404 is not "no releases".** GitHub deliberately answers 404 for a private
repo exactly as for one that does not exist, so an unauthenticated check cannot tell the two apart.
The app reports *no releases* only when it is authenticated and *not visible* otherwise, and the
panel has its own sentence for that in all fifteen languages. Assets now record their API URL
alongside the browser one, because a private release asset is not fetchable from the browser link;
the token comes from `updates.github_token` or `PMS_GITHUB_TOKEN`, is never logged, and goes to no
host but `api.github.com`. The release cache key carries whether we were authenticated, or one
tokenless 404 would be served back to a caller that now has a token.

### The repository itself

* a README with the real UI in it, a full feature list in `docs/FEATURES.md`, and
  `RELEASING.md` - what is worth publishing at all, and what a release contains;
* **`tools/release.py`**, which refuses rather than guesses: the version must already be in all four
  sources, each artifact must *report* that version and be newer than the sources it was built from,
  every gate must pass, and the notes come out of this changelog. `--dry-run` does all of it and
  changes nothing. The asset names are a contract - the app picks a console's file out of a release
  by name;
* **`companion/config.json` is no longer tracked.** It is the owner's live config: this machine's
  device id does not belong in a public repository, and a token would have followed it the moment
  `updates.github_token` was used. `config.example.json` is the template, `start.cmd` copies it, and the app falls back
  to its defaults when neither exists.

### Gates

`tools/test_payloads.py` is at **237 checks**. The three fixes above are pinned separately - fixing
any two still leaves a wrong tile - and each new check was perturbed to red before being kept.

## [3.85.0] - 2026-09-30 - "Any device, whether or not it holds the files" `[VERIFIED]`

The owner copied the exe to a second PC. Every tile there read **"Not on this PC"**, greyed and
unpressable, with no status at all - while the console sitting next to it was carrying all eight
payloads and running five of them. The app is meant to behave as one thing however many devices are
looking at it, and the panel was the one part that did not.

### Two different answers that were being given as one

*"This PC does not have it"* and *"nobody has it"* are not the same, and only the second is a
reason to disable anything. The list route now says **where** each item can be had from - `pc`,
`console`, or a named peer - and a tile is pressable whenever any of them can hand the bytes over.

* **Every payload is on the console already**, because both ELFs carry them and write them out at
  boot. So on a machine with no folder at all, all eight are usable.
* **Status is decided before the file question.** Whether something is running is a fact about the
  console; where its file lives only matters when something has to be sent. A payload running on
  the PS5 now reads *Running* from every device, not *"not on this PC"*.
* **A companion advertises what it holds** in `/api/federation`, and an action that needs bytes this
  PC does not have is forwarded to a peer that does - the same request, one hop further, with the
  peer's own reply passed back. `via_peer` stops two companions bouncing it between them.

### The trap that made "start the console's own copy" fail

Asking a console to start a payload it carries answered **"Payload Manager did not take it"**, every
time. That is the basename trap this repo already documents: `/loadpayload` resolves by **basename**
against pldmgr's own registered directory, not by the path it is handed - so a file in
`/data/pkg-mutant-shop/payloads` is never the one that runs. The console now stages the payload into
`/data/pldmgr/payloads/<name>/<name>.elf` first, which is exactly what `companion/deploy.py` has
always done for our own ELF, and loads that. Copied only when it differs, so a second press costs
one stat.

Verified from a companion configured with a folder that does not exist: **"Started FTP server from
the console's own copy"** on the PS5 and **"Started nanodns from the console's own copy"** on the
PS4, with the machine holding no payload files whatsoever. The PC that does have them is unchanged -
it still starts things without copying when the bytes already match.

Also fixed on the way: the PS5's `write_all` returns `void`, so the staging copy writes in its own
loop rather than silently ignoring a short write and handing Payload Manager a truncated ELF.

`test_payloads.py` is **229 checks**. One of the new ones was a check that could not fail - it
looked for a ternary that survived the regression it was meant to catch - and was rewritten to
compare the FIRST mention of each branch. Perturbed, watched to pass, fixed, perturbed again,
watched to fail.

---

---

## [3.84.4] - 2026-09-30 - "An update that goes everywhere it needs to" `[VERIFIED]`

The owner took the WebKit autoloader 0.5.2 update, the tile showed 0.5.2, and pressing it answered
**"that file is not on this PC"**. Four separate faults were behind it, each found by reproducing
rather than reasoning.

* **The panel and the button were reading different catalogues.** The list route rescans the folder
  live; the action route still read the one baked at build time - so the instant the update renamed
  `…_v0.5.1.elf` to `…_v0.5.2.elf`, the lookup was describing a filename that no longer existed.
  One view of the folder now, for the serve registry too.
* **The console kept the old bytes.** Both ELFs write the payloads they carry to PB_DIR at boot, so
  "start the console's own copy" is normally right - but after an update it would have silently run
  the previous build while the tile showed the new number. Run now makes sure the console has the
  bytes this PC holds before starting anything, and skips the copy when they already match. The
  console's own copy is refreshed too, so the next press costs nothing.
* **The console was asked what it HAD, not what it would RUN.** The first version listed PB_DIR,
  which still contained leftovers from older builds under the same stem - and one of them won the
  size comparison. It reports one entry per bundled payload now: the file `/api/payloads/load`
  would actually start.
* **`qparam()` takes the request PATH, and it was handed the whole request.** It still finds a `?` -
  in the request line - so it parsed a value with `" HTTP/1.1"` on the end, matched nothing, and
  both consoles answered *"this build does not carry that one"* about a payload they were holding.
  Every other caller in both files passes `rawpath`; a check now makes sure they keep doing that.

**And the reply says which of the two things happened.** "Sent X to the PS5" when nothing was sent
is a small lie that makes a 2 MB transfer look like it happens on every press. It now reads
*"Started X on the PS5 - it was already there"* when the bytes were already correct, on both
consoles - the PS4 included, where our own ELF reads PB_DIR and posts it to GoldHEN so nothing
crosses the LAN either.

Verified live: `nanodns` on the PS4 and `ftpsrv` on the PS5 both start from the console's own copy
with no transfer, and the WebKit installer - the one that started this - starts cleanly at 0.5.2.
`test_payloads.py` is **217 checks**, both new ones perturbed and watched to fail.

---

---

## [3.84.3] - 2026-09-30 - "A service that answers nobody can still be seen" `[VERIFIED]`

The owner started **nanodns on the PS4 from this panel, it worked, and the tile stayed grey** -
*"Nothing listens for it, so there is no way to tell"*. It was telling the truth about what it had
tried, and what it had tried was wrong.

**What was measured before changing anything.** nanodns listens on **UDP 53**, so a TCP connect can
never see it. The obvious replacement - ask it a DNS question - does not work either: neither
console answered a query on :53, not for ordinary names and not for the Sony domains nanodns exists
to intercept, **even on the PS5 where Payload Manager confirms it is running**. So there is no way
to observe it from off the console at all.

**What can be observed, from the console, is that the port is taken.** `udp_port_taken()` tries to
bind it; a refusal because the address is in use is proof something holds it, and the socket is
closed either way so a genuinely free port is left as it was found. Deliberately **no
`SO_REUSEADDR`** - with it the bind would succeed alongside the running server and the answer would
be "free" for ever, which is the same permanently-wrong shape as the `9021`-vs-`10101` mix-up.
127.0.0.1 and 0.0.0.0 are both tried, because a server bound to one does not always conflict with
the other and either conflict is proof.

**And the PC now asks the console instead of probing it.** The console is the only one that can run
that bind test, and on a PS5 it also has Payload Manager's process list - which is how kstuff and
ShadowMountPlus are seen. One request gets all of it; probing from the PC got none of it. The old
path is still there for a console too old to answer.

Result, verified live: the PS4 reports `['ftpsrv.elf', 'nanodns.elf']` and the tile reads
**nanodns · Running · 0.4** in green.

`test_payloads.py` is **215 checks** now, including that nanodns is probed by binding and that the
bind test carries no `SO_REUSEADDR`. Both were perturbed and watched to fail.

---

---

## [3.84.2] - 2026-09-30 - "Every payload names its version" `[VERIFIED]`

* **The right upstreams.** The owner runs drakmor's forks, not the originals: `drakmor/ftpsrv`,
  `drakmor/nanoDNS`, and `aydencharles/onionHEN` for OnionHEN - the fork that was configured before
  it had no releases at all, which the panel had been reporting honestly.
* **Every payload shows a version now, and none of them is a guess.** Three carry one in the binary
  (kstuff 1.11, Payload Manager 0.5.2, WebKit autoloader 0.5.1). The other four carry none anywhere -
  and if the file on disk is **byte-for-byte the size of an asset in a release, it IS that release**.
  The owner's `ftpsrv-ps4.elf` is 166,072 bytes and so is 1.16-ng-stable's. So the panel reads FTP
  server 1.16-ng-stable, nanodns 0.4, ShadowMountPlus 1.7beta2, OnionHEN v0.0.13 - each recorded as
  coming from the release rather than from the file. A byte-exact match also means there is nothing
  to update, whatever the version strings look like.
* **The check runs itself when the panel opens**, using the six-hour cache, so the versions are
  simply there. It only speaks up if it found something; the button is for asking again now.
* **The Cancel option is gone.** Pressing the tile again closes the choice, which is where the press
  already was. Run and Send again are the two real answers.
* **The Payloads & Homebrews button sits under the queue** - centred on it to the pixel, at every
  width, with no JavaScript. It was 326px to the left because `.hrow2` is `space-between` and the
  button was its middle child, so its position was whatever the byline and the source bar left over.
  Two attempts to measure and nudge it were worse than useless: the row redistributes the space the
  margin consumes, and an absolutely positioned child is measured from a containing block that is
  not the one it appears to sit in (that cost 138px and a debugging session). The button shares a
  column with the queue now, so "under it" is true by construction.

---

---

## [3.84.1] - 2026-09-30 - "The panel, made to work like a panel" `[VERIFIED]`

Everything in this release is the Payloads & Homebrews panel answering the owner's review of it.

### Updates are a list you act on, not a number

"Check for updates" used to say *"1 have a newer release upstream"* and stop there. It now returns
**one row per item** - title, `0.5.0 → v0.5.1`, and its own **Update** button - because replacing a
working payload is a decision per payload. Pressing it downloads the release asset, verifies it and
puts it where the old file was.

Proven on the real thing: the WebKit autoloader installer published **v0.5.1** the same day, the
panel found it, and the button fetched `webkit-autoloader-installer_v0.5.1.elf` (2,311,424 bytes,
the exact size the release declares), retired 0.5.0, and the catalogue re-read **0.5.1 out of the
new binary** with no rebuild. Both consoles now carry it.

The download refuses three things, each worse than not updating: overwriting before the transfer is
complete (it writes `.part` and only then moves), accepting a file that is **not the thing it
replaces** (the marker that identifies the project is checked *inside* the downloaded bytes), and
leaving two copies behind. The old file is kept as `.bak` until the new one is in place.

GitHub's rate limit is reported as itself - *"GitHub is rate-limiting these checks"* - rather than as
a generic failure, because at 60 requests an hour it is the ordinary answer, not an exotic one, and
it must not read like "this project has no releases".

### The panel

* **One header row**: Back, the title centred, the PS4/PS5 tabs, Check for updates, close. The title
  takes the slack from both sides so it stays centred whatever a translation does to the buttons.
* **Tiles carry state and version, nothing else.** What a thing is *for*, which project it comes
  from and the file it is, moved into the hover tooltip where they cost no height. A payload that
  changes the jailbreak layer keeps a `⚠` on its name, because that is a warning, not a description.
* **Alphabetical**, by what the tile actually says.
* **A press on something already running or already installed asks.** It opens a row naming what
  each next press would do - **Run** / **Send again** / **Cancel** - instead of the press-twice-and-
  hope it replaced. And **Run means run**: both ELFs already wrote the payloads they carry to disk,
  so starting one copies nothing. Verified: pressing Run logged zero uploads.

### It keeps itself current

The PC now rescans the folder whenever its fingerprint changes and serves that, so a file dropped in
while the panel is open appears **in about three seconds with nobody refreshing anything** - measured
by dropping a renamed copy of nanodns into a new folder and watching the grid go from 8 tiles to 9,
identified by content, then vanish again when it was removed.

That is affordable because of what it costs: the folder fingerprint is **0.000 s** when nothing
moved, a rescan is 0.48 s and only happens when it did, `dir_stats` memoises the 3,000-file RetroArch
walk, and the console probes are cached for four seconds however many pages are open. The answer
carries a `sig` covering the folder, what is running and what is installed - an unchanged `sig` means
the poll costs one request and **no DOM work at all**.

### Bugs found by doing it

* **A versioned filename in an `.incbin` breaks the build the first time an update lands.** The
  update produced `…_v0.5.1.elf` and `sync_payload_bins --check` went red pointing at a file that no
  longer existed. The repo's copies are named by catalogue id now - the identity that does not move,
  because it comes from a marker inside the binary - and all three matchers reduce a filename to the
  same stem so the page can still ask using the name the owner sees.
* **Three running payloads read as stopped** the moment those names diverged, because the page was
  still comparing raw filenames. The same class of mistake as matching a process by filename, which
  this panel had already been bitten by once.
* **Two copies of one payload drew one tile.** The grid keyed on the catalogue id, which two files of
  the same project share; it keys on the file now. Found by the live-rescan test.
* **The PS5 ftpsrv would have downloaded the PS4 asset** - one release carries both, and "shortest
  name ending .elf" picked the wrong console. The platform is part of the match now.
* A literal inside `toast(t(cond?"a":"b"))` reads to `i18n_report` as untranslated English, and it is
  right to count it; the key is chosen before the call.
* A test that pinned `0.5.0` turned red when the update feature did its job. It pins the invariant -
  that the version is read *from the file* - not the number.

`test_payloads.py` is **164 checks** now, including that no embedded path carries a version and that
all three stem implementations agree. Both were perturbed and watched to fail.

---

---

## [3.84.0] - 2026-09-30 - "Payloads & Homebrews" `[VERIFIED]`

A new panel in the top bar, next to the byline: **Payloads and Homebrews**, two columns, three to a
row, aimed at whichever console's tab is selected - the same rule the cheats panel follows. The left
column sends a payload; the right installs a homebrew through the engine the games already use.

### What it does

* **Platform-aware, from the folder rather than a filename.** The catalogue is built from
  `C:\Mutant Payloads & HomeBrews` by its shape - `Payloads/PS5/...`, `Homebrews/PS4/...` - so a PS4
  tab shows PS4 things because that is where they were put, not because a name was parsed for the
  letters "ps4". A PS5 package can never be handed to a PS4.
* **State that was observed, never assumed.** A tile is green because a port answered *or* because
  Payload Manager listed the process; a homebrew is green because the console lists the title. A
  payload with nothing to observe says *"Nothing listens for it, so there is no way to tell"* rather
  than showing a light nobody can back.
* **A second press is a different decision from the first.** Sending a payload that is already
  running gives you a second copy of it; installing a homebrew that is already there replaces it; a
  jailbreak-layer payload is yours to start. All three arm the tile and say what the next press will
  do, instead of doing it.
* **It works with every PC switched off.** Both ELFs carry the payloads and the catalogue, and both
  answer `/api/payloads` themselves.

### The measurements that shaped it

The owner asked for the homebrew packages to be embedded too, and suggested compression. Both were
measured rather than argued about. Compression is not the lever: a PKG is a PFS image whose contents
are already compressed, and real samples gzip to 98.9% (Itemzflow), 98.2% (Itemzflow PS5), 92.0%
(FPKGi) and 93.8% (PS4-Xplorer) - about a tenth across the set. And the PS5 ELF was already 34 MB of
which **33.1 MB was blobs**, leaving about 1 MB of code; the packages would have taken it past
300 MB, in an ELF that Payload Manager loads into RAM.

So the split is by arithmetic, not preference. **Payloads are embedded** - PS5 32.6 -> 42.9 MB, PS4
8.7 -> 9.0 MB. **Homebrews are seeded to the console's own disk once** and installed from there for
ever after, because `pkgfile_path_allowed()` already accepts `/data/…*.pkg` and serves it to the
installer over loopback. "No PC needed" does not require the bytes to be inside the ELF; it requires
them to be on the console, and there is no moment when a console has this app but has never met a
PC - the ELF itself has to arrive somehow, and the seed rides that trip.

### Two fields where there was one

`payload_bundle.h`'s `port` meant both *"this is how you tell it is running"* and *"launch it when
that port is free"*. Measured on this console: **Payload Manager lists `shadowmountplus.elf` at a
live pid while `:10101` is closed**, so under one field it looked stopped *and* was a candidate to
be started again on every boot - the same shape of wrong answer the `9021`-vs-`10101` mix-up gave
for months. `port` and `autostart` are now separate, and only ftpsrv and ShadowMountPlus ever start
unasked. kstuff, OnionHEN and the WebKit installer ship at `autostart 0`, which is the rule that
file already stated in its own words: *a payload that changes the jailbreak layer is the user's call
to make, never a side effect of installing our app.*

### Bugs found by building it

* **Payload Manager reports the name a payload was BUILT as, not its filename.** `ftpsrv-ps5.elf`
  runs as `ftpsrv.elf` and `pldmgr_v0.5.2.elf` as `pldmgr.elf`, so matching raw filenames reported
  two live payloads as stopped. Both ends compare stems now.
* **`Ps5Bridge.pldmgr` is a property, not a method** - calling it threw *after* the upload had
  already succeeded, so the file landed and the press still said it failed.
* **`classList.toggle(name, force)`** does not work in the console's browser; `check_web.py` caught
  it before it shipped, which is what that gate is for.
* Walking back from `"contentId"` to the nearest `{` lands on the **sibling** `ageLevel` object,
  which is valid JSON and parses happily - it just is not the right object. That is how the Internet
  Browser package gets its real name out of the `FIH` container it hides inside.

### Identity comes from the file, not its name

The owner renames things, and said so plainly. So a payload is recognised by a **marker inside the
binary** - the ftpsrv protocol banner, `Kstuff Lite`, `WebKit Autoloader`, `Payload Manager v` - and
keeps its port, its upstream and its jailbreak warning whatever the file is called. Proven by
renaming `kstuff.elf` to `totally-different-name.elf`: still identified as kstuff, still version
1.11, still marked jailbreak-layer.

Three of them carry a **readable version inside**, which now beats the filename: kstuff 1.11,
Payload Manager 0.5.2, WebKit autoloader installer 0.5.0. The rest have it only in the name, and the
catalogue records `version_from` so the panel never presents a guess as a fact.

Homebrew packages are matched to the catalogue **by exact byte count**, for the same reason.

### Everything works with no PC at all

* Both ELFs carry the payloads and the catalogue and answer `/api/payloads` themselves.
* `/data/payloads` is created on the PS4 and the bundled payloads are mirrored into it - that is the
  folder people drop an ELF into over FTP or from a USB stick, and it now exists with the right name
  and something in it. We never load from there; `PB_DIR` stays ours.
* The console searches **its own drives** for homebrew packages - our data folder, `/mnt/usb*`,
  `/mnt/ext*` - so a stick with packages on it works with nothing else running. On the first live
  run it immediately found three the owner already had.
* A console reports the title ids it has installed, so a homebrew already on it says **Installed**
  on the console's own page.
* A payload the PC does not have is started from **the console's own copy** instead of being
  refused, which is what makes the panel work from a PC that never had the owner's folder.

### Proven end to end on the real consoles

* Itemzflow seeded from the PC to `/data/pkg-mutant-shop/homebrews`, installed **from the console's
  own copy with no PC in the install path**, and confirmed by the only thing that counts:
  `bgft.db` row 86, `ITEM00001`, **status 1036**. The panel then showed it green on the PS5's own
  page.
* The Internet Browser package went down **both** lanes - loopback and PC-served, the identical call
  a game makes - and the console refused it both times (`0x80B21103`, `0x80B22400`). That is the
  owner's chosen behaviour working: list it, hand it to the engine, let the console decide.
* ShadowMountPlus sent from the panel and confirmed running at pid 104.

### More bugs found by building it

* **A payload's mirror sat after an early `continue`**, so it was only written on a boot where the
  primary copy had changed - which is never, after the first. `/data/payloads` stayed empty while
  our own folder had both files. Measured on the console, then fixed and re-measured.
* **A 900-byte buffer truncated the installed-title list at 72 entries**, dropping the very homebrew
  that had just been installed - so the panel said "Install" about something already there. This
  console reports 117.
* **`/api/payloads` already existed on the PS5** (it reported the boot log), so the one added for
  the panel was unreachable dead code behind it. Both answers come from the one route now.
* Three C ordering mistakes the compiler caught - a function defined inside another function, and
  two used above their definition.

### Also

* The owner's **ShadowMountPlus replaces the one we shipped** (2,437,704 bytes vs 2,465,896).
* `THIRD-PARTY-NOTICES.md` records all six newly embedded binaries, and says plainly which licences
  were **not** verified rather than guessing them.
* New gates: `gen_payload_catalog --check`, `sync_payload_bins --check` and `test_payloads.py`
  (118 checks), all three wired into `ready_check`. Each was perturbed and watched to fail.

---

---

## [3.83.3] - 2026-09-28 - "The icon was a build behind" `[VERIFIED]`

**The four notes were removed in 3.83.1 and 3.83.2, but only for whoever starts the shop from the PC.**
The PS4's home-screen app carries its own copy of the payload, and that copy was still the 03:11
build - so pressing the icon served a panel with all four sentences back.

The PS4 side is three artefacts in a line, and `onconsole/build-wsl.sh` only builds the last one:

    0  pms-agent.prx                   the in-game agent, embedded by both payloads
    1  PKG-MUTANT-SHOP-PS4-LITE.elf    the shop, with no package inside it
    2  IV0000-PKGM00001_00-....pkg     the home-screen app, carrying (1)
    3  PKG-MUTANT-SHOP-PS4.elf         the shop, carrying (2)

Rebuilding (3) alone re-embeds whatever (2) already was, so the shipped payload was current and the
icon inside it was three versions old - and there is no gate that compares the page inside the tile
package with `web/index.html`, so nothing said a word. `bash ps4-app/build-all-wsl.sh` builds all
four in the one order that works; that is what this release is.

**Why it mattered more than "the icon shows an old panel".** `extract_web()` writes every embedded
web file to `/data/pkg-mutant-shop/web` with `O_TRUNC` on load (only `config.js` is spared, because
deploy writes the companion URL into it), and `serve_static()` serves that directory. So a boot from
the stale icon did not just render an old page for itself - it **overwrote the page on disk**, for the
console's browser and for any PC pointed at that console, until a current payload was loaded again.

---

---

## [3.83.2] - 2026-09-28 - "The line that duplicated the picker" `[VERIFIED]`

The fourth note out of the same section, asked for once 3.83.1 made it visible. **"Cheats in your
library exist for: 01.00, 01.26, 01.33_2."** was written as the *tail* of the version-mismatch
paragraph, so removing the paragraph left it standing on its own directly above the version picker -
which lists the same versions as buttons that can actually be pressed. Two lines for one fact, the
weaker one first.

A mismatched file now draws nothing of its own: the header says `Version mismatch <file>` in the warn
colour, the line beneath it gives `file v01.26 / installed v01.33`, and the picker names every version
the library holds. Nothing was lost - every fact those notes carried is still on the screen.

**The arm is empty, not collapsed.** `unsure` is `okv && no installed version`, so it cannot be true
while `okv` is false and a plain `if(unsure)` would behave identically *today* - but that is a fact
about how `unsure` is computed three lines above, and this exact chain has already been broken once by
reasoning that way about a neighbouring branch. `if(!okv){ }` consumes its own case and cannot become
wrong when something near it changes. `test_panel_console_state.py` pins four removed call sites and
three branch shapes now, each perturbed and watched to fail.

---

---

## [3.83.1] - 2026-09-28 - "Three fewer things to read" `[VERIFIED]`

Asked for by the owner, and nothing else in the section was touched. Three notes are gone from the
game panel's cheats area, on both platforms:

* **"This cheat file was built for game version 01.26, but your game is 01.33."** The header
  directly above it already says `Version mismatch <file>` in the warn colour with
  `file v01.26 / installed v01.33` on the line beneath, so the paragraph restated a fact the panel
  had just stated twice. What the header does *not* carry stayed: the line naming which other
  versions the library holds, because that is what sends them to the version picker. **That line was
  removed too, hours later, in 3.83.2 above** - standing on its own it duplicated the very picker it
  was pointing at.
* **"The in-game helper is already on for this game. Start it and the cheats will work."** An armed
  PS4 title now draws nothing at all here - no note and no button. The helper has armed itself since
  3.76.0; a panel that narrates a thing it did for you is noise.
* **"Mods are listed only - launch the game to turn them on."** The controls say this by being
  disabled. The patches wording is kept, because nothing else on the panel says a game patch needs
  the game running.

**The branches stayed.** Each of those notes sat in an if/else-if chain that decides what the
section says next, and removing a note by collapsing its condition hands the case to the arm below -
which is exactly how the master-code note once stopped a title being told to install the game first.
So the not-running arm still consumes the not-running case and the armed arm still suppresses the
button; only the sentences came out. `test_panel_console_state.py` now pins both halves - the three
call sites must stay gone *and* the two branch shapes must stay - and each was perturbed and watched
to fail. The dictionary entries are left in all fifteen languages as dead weight rather than
deleted, so the wording is one line away if it is ever wanted back.

---

---

## [3.83.0] - 2026-09-28 - "The master code is gated like everything else" `[VERIFIED]`

**This fixes a crash 3.82.0 caused, and explains the one that came before it.** The owner reported
both plainly: Dark Souls II's 01.02 cheats used to activate and then crash when they hit an enemy;
after 3.82.0 they crashed *instantly, on pressing the cheat*.

### One cause, two symptoms

The master's cave begins `48 89 1D F9C7B8FD` - `mov [rip-0x2473807], rbx` - issued from +0x2077807,
which resolves to **image offset -0x3FC000, absolute 0x4000**. "1 hit kill" reads the same slot with
`cmp r15, [rip-0x2473876]`. Measured through the in-game agent on the owner's console: **0x4000 is
ST_UNMAPPED** while 0x400000 reads fine. The trainer needs eight bytes of scratch below the
executable and there are none.

    before 3.82.0   "1 hit kill" installed its own cave, which READS that slot, so the game faulted
                    when the damage path first ran = "crashes when I hit an enemy". The other five
                    cheats were silently refused - their cave was still empty, so the gate saw zeros
                    where the file documented the master's instruction.
    3.82.0          also installed the master, whose hook sits in a path that runs constantly. The
                    cave ran at once, wrote to 0x4000, and the game died the instant a cheat was
                    pressed.

So the blind write did not introduce the fault. It made the same fault fire immediately and reliably
instead of on a hit. **The crash was in the cheat file all along and nothing was checking.**

### What the master engine shipped that was wrong on its own terms

```c
if (mem_read(pid, addr, cur, (size_t)wl) != 0) { bad++; continue; }
if (memcmp(cur, w, (size_t)wl) == 0) { skipped++; continue; }   /* already in place */
plan[i] = 1;
```

Nothing between "the read worked" and "write it". A master entry documents no original bytes, so
there was no expect-gate to fall back on - and every other write in this engine has one. On a build
the file did NOT fit, that would put a jump into arbitrary code. It is not what happened here (the
console reports APP_VER 01.02 for a 01.02 file, and both hook sites hold exactly the bytes their cave
re-executes), but it is what the code permitted, and it is gated now.

**A correction, because this was stated the wrong way round first:** 0x5BCBA5 was first read as an
absolute address rather than base + offset, found `5D C3 48 8B 03` (`pop rbp; ret`) there, and wrote
it up as "a jump landed on pop rbp; ret in a build the file was not for". Those bytes came from the
wrong place. `/api/mem/read` takes an absolute address; cheat offsets are image-relative.

Measured across the whole shipped library: 8,126 cave mods and 20 master blocks contain 265
identifiable RIP-relative operands. 260 resolve to a non-negative image offset. The other five all
resolve to exactly -0x3FC000, in three files (`CUSA01589_01.02`, `CUSA07439_01.00`,
`CUSA07439_01.03`). Five identical values is the authoring tool's convention, not decoder noise -
which is what makes this checkable rather than guesswork.

### The four gates

Nothing is written unless all four pass, for the master **and** the mod, decided before anything is
touched:

| | |
|---|---|
| **CAVE** | an entry writing a routine into space the file treats as empty must find it EMPTY - all zeros - or already holding what it is about to write. A mismatched build would have real bytes there. |
| **HOOK** | an entry patching real code with a jump into one of this master's own caves must find bytes that appear VERBATIM inside that cave's body. A trampoline re-executes the instruction it stole, so this is the invariant that says "right site, right build". All 21 hooks in all 20 master blocks jump into a cave the same master writes, and 19 of 21 return to hook+len - so it is the structure the format has, not one invented here. |
| **REACH** | every RIP-relative target in a run about to be written must be a non-negative image offset AND readable. This is the one that catches the 0x4000 scratch. |
| **TOGETHER** | master and mod are decided first and written second. A mod that is going to be refused can no longer leave an irreversible master behind - and a master that cannot be installed refuses only the mods that sit INSIDE its caves, so a plain byte patch elsewhere in the same file is untouched. |

A cave-resident cheat's documented "off" bytes *are* the master's bytes, so the mod's gate reads
memory and then overlays the master's **plan** before comparing (`master_overlay`). Without that,
deciding first would refuse every such cheat; writing first is the bug being fixed. Both, and
neither, were wrong.

### Asked, not told

The engine could apply a cheat and it could refuse one, and there was no way to ask what it was going
to do without letting it. That is why a crash was the first evidence. `cheat_apply_blk` now takes
`check_only`, exposed as `GET /api/cheat/apply?...&check=1`: every read, every gate and every reason
identical, returning before `mem_write` is ever called, with the same `detail` string - so a
diagnostic and the real thing cannot disagree about why something was refused.

**It is how this fix was proved on a real console, with the game running and nobody touching it.** All seven Dark Souls II cheats, asked:

    mod 0 God mode              rc=-6  master_refused=5   needs a scratch space this game does not have
    mod 1 infinit Stamina       rc=-6  master_refused=5
    mod 2 infinit Souls Memory  rc=-6  master_refused=5
    mod 3 Level max             rc=-6  master_refused=5
    mod 4 Infinit Souls         rc=-6  master_refused=5
    mod 5 1 hit kill            rc=-4  noreach=1          its own code reaches nowhere
    mod 6 infinit Consomable    rc=1   would_write=1      this one is right for this build

and then every byte re-read: `5BCBA5`, `5C3F70`, `2077800`, `19054E`, `107D1B1` all exactly as before.
Then mod 6 applied for real (`4429F0` -> `83E800`), read back, reverted, read back. The game was still
running and byte-identical to how it was found.

And finally the path the owner actually presses - `POST /api/mods/CUSA01589/toggle` for God mode, the
thing that crashed the game in 3.82.0:

    ok=false  rc=-6
    "This cheat needs a scratch space in memory that this game does not have, so nothing was changed.
     It was written for a setup this console cannot give it, and forcing it would crash the game."

with `5BCBA5` still holding `8B8370010000`, the cave still zeros, and Dark Souls II still running.

### So what does the owner get on Dark Souls II

One working cheat, six honest refusals, and no crash. Before 3.82.0 they had one working cheat, one
that crashed on hitting an enemy, and five that silently did nothing.

**And the six cannot be made to work as written** - not by us, not by anything, until something
provides that scratch page. It is NOT a version problem: the console reports 01.02, the app picks the
01.02 file, and it reports `compatible: true, "exact version"`. The panel now says which of the two
reasons applies, in words, rather than "the cheat file was made for a different version of the game".

The route that would make them work is written up in `research/cheat-formats.md`: the slot is a single
qword, every reference to it sits inside a run we write, and there is zero-filled padding at the end of
the image - so the displacement could be pointed at a slot inside the game. It is not implemented
because a scratch slot has to be **writable by the game**, and nothing available today says which of
the image's segments are (the agent's map carries ranges, not protections, and it makes a page writable
for its own write and then restores it). Choosing a slot without that would be the same class of guess
that caused this. The missing piece is segment protection in the agent's OP_STATUS, plus a live test on
a console somebody is sitting at.

### Also in this build

* **`Disable-all` now removes a removable master code.** The panel has been promising that in 15
  languages since 3.82.0 and nothing called the removal path - `cheat_master_apply(want_on=0)` had no
  caller. A sentence on a television that the code does not implement is worse than no sentence.
* **Removing a master expects to find it there.** The first version compared against the wrong state
  and answered "the space it writes into is not empty" - which is true by definition when the thing
  you are removing is in it. Caught by the test, not by reading.
* **`tools/cheat_doctor.py` explains the scratch.** It decodes the same operands the engine does, with
  the same prefix table, and prints `UNREACHABLE: 48891d reaches image offset -0x3fc000, below the
  executable` against the master and against "1 hit kill". It also reports the master block itself, and
  says plainly that a master entry is not judged the way a mod is, so its NEITHER column is not by
  itself a fault.
### The missing primitive is no longer missing: a signature search

`research/cheat-formats.md` has named the same gap three times - "a multi-pattern sweep of a module,
done on the console, in one pass" - because three separate features need it and none can exist without
it. Both payloads now answer:

    GET /api/mem/find?pattern=48??8B05[&pid=][&base=][&from=][&to=]   -> starts it
    GET /api/mem/find/status                                          -> progress + image offsets
    GET /api/mem/find/cancel

Read-only, one search at a time, in its own thread, through the same `mem_read` both consoles already
have - so no new capability and nothing written. `??`, `**` and `xx` are all wildcards, so the game-patch
XML's own mask syntax pastes in unchanged. Offsets in and out are image-relative, like a cheat file's.
A signature must be at least 4 bytes with 3 real ones; all-wildcards is refused, because it matches
everywhere.

**Measured on the owner's running Dark Souls II, read-only, while they slept:**

    64 KB around a known site    0.5 s, found 5BCBA5 - the hook site, exactly
    the whole 34 MB module       229 s, 6 matches, 31 gaps, no errors
    the shop during that sweep   /api/health worst 0.02 s; /api/mods worst 3.40 s, 0 failures

The 3.4 s is the point of the millisecond nap between chunks: `/api/mods` shares the agent's channel,
and a sweep that starved it would make the shop look broken while it ran. It slows; it does not break.

**And it found something worth knowing on its own.** The six bytes `8B8370010000` from that hook site
appear **six times** in the module (2FCA26, 5BCBA5, 84AF1F, 84B304, 962429, 154CA9A). A porting tool
that took the first hit would write a cheat into the middle of an unrelated function - which is exactly
why `cheat_port.py --by-signature` demands **exactly one** match and refuses otherwise, saying how many
it found. Tried live against the running 01.02 game with the 01.00 file's "Max Souls": two matches,
refused, offsets printed.

**`tools/cheat_find.py`** is the friendly face of it, because "where is this code?" is the first
question of making a cheat from scratch:

    python tools/cheat_find.py CUSA01589 "8B 83 70 01 00 00 C5 FA 10 83"
      searching 2,097,152 bytes for 8B8370010000C5FA1083  (this takes about 13s on a PS4)
      1 match(es) in 15s, 1 unreadable gap(s)
         5BCBA5
      ONE MATCH: 5BCBA5 is an address you can use.

The same site with only six bytes gives six matches and the tool says "NOT AN ADDRESS - lengthen it".
That is the whole workflow: extend the pattern until exactly one survives.

These are console routes, deliberately not proxied by the PC and not wired into any button. A
three-minute sweep is not something to put behind a tap, and the tools talk to the console directly
the way `cheat_doctor.py` already does.

* **The exe gate now checks that what the exe CARRIES is what was built.** It only checked the four
  bundled files were present. Found by looking: after the PS4 payload changed, the shipped exe still
  carried the previous one - 9,135,272 bytes embedded against 9,135,448 on disk - and the dashboard-app
  package was a build behind as well. `ship.py` hashes the three files it copies, so the loose payload
  beside the exe was current while the copy inside it was not; on another PC the exe is all there is,
  and it would push that older payload to the console without a word. The gate extracts each bundled
  file out of the PyInstaller archive and compares sha256 (a raw byte search cannot: the archive is
  compressed, measured). It caught both immediately, and this build's exe was rebuilt because of it.
* **The console's content-change signal is debounced.** Measured in the console's own log: one tile
  install moved `apps_sig` three times in four seconds, and each one re-read app.db and re-decided the
  owner's plugin list. Re-deciding writes nothing when the answer is unchanged, so it was waste rather
  than damage - but waste inside a system daemon whose heap we do not own. Ten seconds now, with a
  pending flag so a change during the cooldown is not lost. Verified live: one reconcile for the whole
  install instead of three.
* **`tools/test_cheat_core.py` is now 125 checks**, including the real shipped Dark Souls II file with
  the memory state measured off the console, the crash reproduced as a fixture (`5DC3488B03488B4030`
  at the hook site) and asserted to write nothing, and check-only asserted to agree with the real
  apply on both a success and a refusal. Each of the six new gates was perturbed and watched to fail:
  removing them costs 10, 1, 1, 5, 5 and 4 checks.

---

### The audit, and the 30 defects it found

A full adversarial audit of every change 3.82.0 made: each area read independently, each finding
re-checked from three separate angles with a majority needed to confirm it, and a final pass asking
what nobody had looked at.
**61 findings survived verification and 10 were rejected.** Its line numbers were routinely wrong (it
cited the signature scanner for the master engine), so every one below was re-read before it was
believed. The ones that mattered are fixed; the rest are listed here so they are not lost.

**In the master engine shipped hours earlier:**

* **Only one cave-resident cheat per file could be on at a time**, and the second was blamed on the
  game's version. The idempotence test was an exact memcmp of the whole cave - but a cave is BY DESIGN
  mutated by the cheats living in it; that is what makes them diffs against it. Measured: 43 mod
  entries across the 20 master-bearing files start inside a master span, 9 of those files have two or
  more sharing one, and CUSA05574_01.50 has eight in a single cave. A byte may now differ where some
  mod in the same document declares a run - the positions the file itself licenses. A wrong build
  still fails, because its bytes differ everywhere.
* **cheat_master_commit carried on after a failed write** and reported "Nothing was changed" - while a
  hook could already be sitting over a cave that never got filled. It stops at the first failure, says
  how many landed, and the sentence tells the owner to close the game.
* **A master entry the parser could not read was silently dropped** and the rest written anyway - half
  a routine, which the mod path refuses in those exact words.
* **cheat_master_span took the first `{` after the token "master"**, and 21 cheats in the library have
  "Master" in their NAME. It requires the key form now.
* **Two ~1 MB leaks**: the mod-refusal early returns never released the master plan, on the accept
  loop, in a payload whose heap this project has already exhausted once.
* **run_unreachable worked in image-offset space** while the same offset may be an ABSOLUTE address
  (abs_mode, or a per-entry `<Absolute>`), so for .shn and .mc4 files it resolved the operand from the
  wrong place. It works in absolute space now, decides by READING the target, and reads twice before
  believing it - one lost request over the agent's channel must not become a permanent verdict about a
  cheat file.
* **rip_targets was prefix-major under a shared 24-slot cap**, so one prefix filling every slot meant
  later kinds were never scanned - by a check whose whole job is not to miss one. Position-major now.
* **`force` could write a cave-resident cheat into an empty cave** - the original crash, on purpose.
  force overrides the byte gate; it is not a way back to a jump into nothing.
* **`section` was counted and never refused.** 306 entries in 103 files carry another module's index
  and 305 of those addresses pass ADDR_OK, so the engine computed base + offset and wrote there when
  the bytes happened to match. The message has claimed since 3.82.0 that they are declined.
* The expect gate read only `wl` bytes, so a longer documented opposite could never be compared - which
  made a Trainer master with a longer `on` permanently unremovable. Removing one also compared against
  the wrong state entirely, and answered "the space it writes into is not empty" about the thing it was
  removing.

**Older defects the same reviewers found while they were in there:**

* **A brace in a cheat's NAME moved the block boundary.** Ten mods in the library have one; nine are
  balanced and survived by luck. CUSA29102_01.01's `Max Items {after using have 2)` is not, and the
  engine walked **4 of that file's 5 mods** - with "Max experience" swallowed into the block before it.
  A block that has eaten its neighbour applies BOTH memory arrays, so pressing one cheat wrote another
  the owner never touched. next_mod_block and find_mod_block skip strings now, like cheat_master_span
  always did.
* **console_titles_cached took g_scan_lock while its callers already held it.** All three library
  builders call it from inside a wrapper that holds that lock, and the TTL is five seconds - so this
  was the ordinary path. A statically initialised mutex here does not block on a second lock by the
  same thread; the inner unlock handed the caller's critical section away, mid-scan, over shared
  buffers. The lock only ever protected one static, so that static is on the heap and the lock is gone.
* **Five callers passed a function-local `static` as the output buffer**, shared by every thread in the
  function - two of them with no lock at all.
* **Nothing serialised plugins.ini.** Five functions mutate the owner's plugin list, each a
  read-modify-write through one fixed `.part` path, with three independent triggers on a threaded
  server. Two at once shared that scratch file and whichever renamed last published a mixture. It is
  not our file: every other plugin they run is in it, and it survives a re-jailbreak. One mutex now,
  with `_locked` variants for the calls between writers.
* **rest_scan read a process's "name" unbounded**, so an entry without one took the NEXT process's name
  while keeping its own pid - and that name is what decides whether to STOP that pid.
* **A queued update could never finish**: `/api/queue/start` released a held row without deriving the
  category or the expected size, so the finished check looked for a game's app.pkg, which an update
  never writes. **Retry re-registered with no content id and size 0**, because it read fields only the
  queued lane filled, and it **published the previous failure's error code and percentage**.
* **The stall detector could never fire for an update, an add-on or a reinstall** - the guard added
  hours earlier asked whether the console LISTS the title, which for anything but a first install is
  true before the transfer starts. It asks whether the package file has moved now.
* **36 of the 73 Trainer files carry more than one `<StartUP>`** and only the first was converted -
  half a routine again. The cheatline loop also bounded where a line STARTS but not where it ends, so
  it could read a following `<Cheat>`'s bytes into the master.
* **Four .shn files are UTF-16** and converted to a well-formed document with no cheats in it, which
  reads as "this game has nothing" rather than "this file cannot be read".

**On the page, all mine:**

* **The panel re-point rebuilt the whole drawer on every library refresh**, destroying live cheat
  controls under the reader's finger and their scroll position - the exact shape this repo already has
  a memory about. It re-points the object always (free) and redraws only when a fact the panel shows
  has changed, and only while the drawer is open. It also matched on `title_id||name`, which collides
  for the packages the library files with a null title id.
* **gridSignature omitted state.consoles**, the other input gridStateOf reads, and **gridStateOf
  dropped state.installed** - so selecting a platform tab made a title the console had just reported
  as installed read "not installed". It only trusts the per-console answer when there IS per-console
  data.
* **The master note broke an else-chain**, so a title with a master code was never told to install the
  game first.

**And in the companion and the tools:**

* **One install raised two "Library updated" toasts** - `_finished_installing` bumps the gen, and
  forty-five seconds later apps_sig catches up and bumps it again. The owner has said twice that one
  message is enough. Only the echo of our own install is suppressed; two console-side changes a minute
  apart are two things that happened.
* **The first real change after a console's id was learned was thrown away**, because the memory key
  moves from the address to the id on that pass and the new key had no previous value.
* **pack_cheats --check compared file NAMES only**, so a cheat file edited in place - which is exactly
  what porting one does - left the archive "current" while the exe went on shipping the old bytes.
  Sizes and CRCs now, straight out of the zip directory.
* **The signature search's hit cap did not set `truncated`**, so a partial match list was reported as
  complete; **a sweep that read nothing was reported as "not in this build"** by both tools, although
  the status carries a gap count; and **the PS5 swept a blind 64 MB from a hardcoded 0x400000** while
  the PS4 asks the agent for the real base and size.
* **Two checks that could not fail**: `ok("not running" in out or "no cheat file" not in out)`, whose
  second half is true of almost any output, and a survivability check that returned on a first sighting
  before ever reaching the code it claimed to test. Both now assert by name - and the second one
  immediately found that the signal reported "changed" while doing nothing when there was no fleet.

Every fix above is covered by the tests, which are now **200 checks** in the engine harness alone, and
each new gate was perturbed and watched to fail before being believed.

---

## [3.82.0] - 2026-09-27 - "The master code, and three checks that could not fail" `[VERIFIED]`

3.80.0 and 3.81.0 were interim builds inside one long live debugging session on the owner's own
console and never got their own entry; everything from them is folded in here, which is also the
build both consoles are running.

Every number below was measured, on the shipped library or on the console. Nothing here is inferred.

### Dark Souls II's "1 hit kill" crashed because a master code was never applied

Reported: enable it, hit an enemy, the game dies. Not when the cheat goes on - when the game next
runs that path, which is the tell.

`assets/cheats/json/CUSA01589_01.02.json` has a top-level `"master"` block that the engine had never
looked at. Mods were found only inside the `"mods"` array. Decoding the two RIP-relative operands
settles it:

    master  48 89 1D F9C7B8FD   mov [rip-0x2473807], rbx    at +0x2077800 -> base - 0x3FC000
    cheat   4C 3B 3D 8AC7B8FD   cmp r15, [rip-0x2473876]    at +0x207786F -> base - 0x3FC000

The cheat's cave compares against a scratch qword, and the only code anywhere that writes it is the
master's first instruction. Without the master that comparison reads whatever was in memory, so the
branch and the write to `[r15+0x170]` after it are undefined.

**And it was worse than a crash.** Five of that file's seven cheats are byte patches INSIDE the
routine the master installs - God mode is `8B 83 70 01 00 00` -> `8B 83 78 01 00 00` at +0x2077807,
seven bytes into the master's 40-byte block - and their documented "off" bytes are the master's own
bytes, byte for byte. With no master the cave is zeros, the gate sees zeros where it expects the
master's instruction, and the mod is **refused, silently, for ever**. Five of seven cheats in each of
these files were unusable and nothing said why.

The engine now installs the master before the first mod in such a file, whole or not at all, and
skips it when it is already there (so a second cheat costs four reads and no writes). Both formats
carry the idea and neither was read:

    json  "master": { "challenged": "yes", "memory": [...] }                 20 files
    shn   <StartUP Text="Master Code 1 (Must Be On)">                        73 files, 126 blocks

`shn_xml_to_json` now emits `<StartUP>` as `master` and `<Section>` as `section`. The shn form carries
`ValueOff`, so a master that came from a Trainer file can be removed again; the json form documents no
original bytes for the two places it patches, so closing the game is the only way to clear it. The
Mods panel says which of the two, in one note, in all 15 languages - a note and not a toast, because
this happens on the first toggle for 93 games.

### A mod is applied all of it or none of it

Separate from the master, and still a real fault: `cheat_apply_blk` wrote entries one at a time and
carried on past a failure, reporting "written=1 failed=1" as a partial success. For a two-piece hook
(36 bytes of routine into a cave, then a jump into it) that is a jump into memory nobody wrote. The
file already refused a mod it could not fully PARSE for exactly this reason; the same now applies to
an entry that fails at write time. Every entry is decided first, then written. Costs nothing: the
gate already read every entry before writing it. `force` still overrides and says so.

### A key could be read from the object next door

`parse_mod_entries_ex` asked for `offset`, `on` and `off` with an unbounded `strstr` from the start of
the entry. Every one of the 25,888 entries in the library carries all three, so the first match was
always the local one - for mods. A master block documents only `on`, and its "off" resolved to the
first MOD's off bytes further down the document. That is how a master the file says cannot be removed
was "removed", into the wrong address. `json_str_after_lim` bounds the lookup to the object that owns
it. Found by a test, not by reading.

### "It did not work" was three different problems saying the same sentence

Three failure paths all counted as one `failed`, and the app then said "the cheat file was made for a
different version" for all three. Only one of them means that.

    bad_addr     the cheat points outside the process  (a section/absolute entry)
    unreadable   the engine could not read there       (on PS4: the in-game helper has stopped)
    mismatch     the bytes are neither documented state -> the version answer, and only here

`section` is now parsed too - and it is **quoted** in every one of the 306 entries that carry it, so
the first attempt (`strtol` after the colon) read 0 and would have been a check that could never
fire. 306 entries in 103 files name another loaded module, which this engine cannot place; the
refusal says that instead of blaming the game.

### The PS4's stall detector had two inputs and both were dead

    3325:   if (have) { g_job.done = done; g_job.total = total; }
    3354:   if (have && done > g_job.done) g_job.last_move_ms = now_ms();

`g_job.done` was assigned `done` twenty-nine lines earlier, under the same lock, with nothing in
between. `done > done` cannot be true. The second input compared the file on disk against the
START-OF-JOB snapshot rather than the previous poll, so it fired on every poll once the file had
changed once (a dead transfer could never be detected) and never at all while BGFT was still staging
into `/user/bgft/task/<id>/` (a healthy download was on a 15-minute wall clock from handoff). What
hid it was the `console_lists_title` guard beside it: the console registers a title early, so the
deadline expired and produced no error.

Now: the previous byte count is captured before it is overwritten, and a last-seen pair sits beside
the start-of-job snapshot, which `replaced` still needs.

### An install that finished was reported as stopped at 99%

BO3 downloaded, installed and ran while the queue said it had stopped. The verdict came from one
fact - the size of `app.pkg` against the package we handed over - and a console writing a 43.6 GB
base game does not owe us a file within 2%. There is now a second route, deliberately three facts
together because no one of them is safe: the console says it moved every byte, the package file
really changed, and the console's own database lists the title. Each alone is a known false positive;
all three is what "installed" means. The database read happens only when the cheap size test has
already failed, so the polling path is untouched.

### The Updates section now reacts to an install, an update or a delete

`library_gen` - the only thing that makes the page re-read - was bumped by `Library.scan()`, which
the watcher calls when the PC FOLDER's signature changes, and by `_finished_installing`. Nothing else.
Installing, updating or deleting on the console changes nothing in the PC folder, so anything done
from the console's own menu, or a Store download, or an add-on, was invisible until something else
happened to trigger a reload.

Both payloads' `/api/health` now carry `apps_sig` - a stat of `app.db` and `addcont.db`, two syscalls
- and the companion bumps `library_gen` when that string moves, on the health poll it already makes
every 45 s. No extra console traffic and no app.db pull. The PS4's own watcher uses the same signal to
re-decide which titles carry the in-game helper, which also covers a DELETE: the install-completion
branch fires on install only, so uninstalling a game used to leave the helper listed for a title that
no longer existed until the next reboot.

And two page-side reasons it would not repaint even with fresh data:

* `gridSignature()` carried only the title, `on_console` and the PC file's size. The badge that says
  "Installed" versus "Update" is decided by `installed_version`, which is not any of those - so a
  genuinely new library document produced an identical signature and `renderGrid()` returned early.
  The next reload painted the cache, which was by then the fresh one, and it looked right. That is
  "refresh a couple of times" exactly. `installed_version`, `console_size` and the per-console record
  are now in the signature.
* an open panel held the object it opened with. `loadLibrary()` replaces every object in
  `state.games`, and nothing re-pointed `state.current` - so a panel open while an install finished
  kept the pre-install version, size and pending-update list for its whole life.

### The grid's badge now answers the question the filter asks

The owner asked for "whatever is best for the grid". With the platform filter on "All" the card is
about the household and the fleet answer is right, unchanged. With PS4 or PS5 chosen it is about that
machine, and it now uses the same `stateOfOn()` the panel uses - so a card and the panel one tap away
cannot disagree. Grounded, installed on both consoles with only the PS5 on 01.14, reads "Update" with
the PS4 filter on instead of "Installed". Two consoles of one platform means the filter does not name
one, so the fleet answer stands rather than guessing.

### The helper stopped offering a button for something it had already done

The in-game helper arms itself for every installed title the library has cheats for, so "not in the
game yet" usually means "armed, and it will load when you start the game". The panel showed the same
manual control for that as for a title autoarm will not touch. It is a note now, and the button stays
only where it is the only way.

### Checks that could not fail, found and fixed

* **`tools/test_job_claim.py` had 10.0.0.87 baked in as its default.** The PS4 has been at 10.0.0.86
  since it moved house, so every run printed "no PS4 answering - skipping" and passed. With the
  address asked of the companion, it also turned out to refuse on `active` alone - which after any
  install (including the icon repair the payload does at boot) is every console. It now uses the
  payload's own rule, runs against the real console, and passes: 1 of 12 concurrent presses reaches
  registration, 11 are turned away, the row is restored, the slot is not wedged.
* **the section parse** would have been unfireable, as above.
* **both new test files are perturbation-tested**: removing the master apply fails 10 checks,
  removing the quoted-section skip fails 3, and disabling the atomic refusal fails 10.

### New

* **`tools/test_cheat_core.py` + `test_cheat_core.c`** - the real engine, compiled on the PC against a
  fake game whose memory lives in this process. 87 checks, including Dark Souls II's own shipped file
  (all four master entries, master before the hook jump, God mode applying at all) and a sweep of all
  1,761 Trainer files through the converter: 15,378 mods, 73 with a master code, all 73 removable.
* **`tools/cheat_doctor.py`** - does a cheat fit the game that is running, and is it portable. Reads
  every byte run out of the RUNNING game and says whether it holds the documented off bytes, the on
  bytes, or neither. Proven live on BO3: Inf Health and Inf Ammo read as applied, Inf Money as ready.
* **`tools/cheat_port.py`** + **`tools/test_cheat_port.py`** (29 checks) - moves a cheat from one
  build to another using cheats present in both files as anchors. Measured: 87 titles have two or
  more comparable versions and for 70 of them one constant delta explains every anchor. It refuses a
  code cave, a relative jump, a cave-resident cheat, a disagreeing anchor set, and writing on a
  single anchor without `--verify` or `--force`.
* **`research/cheat-formats.md`** - every format we handle, measured: json, shn, mc4 (AES-256-CBC, key
  and IV recorded), and the PS-Game-Patch XML with all ten `Line Type` counts. Including what the
  engine refuses and why, and the one primitive still missing - a multi-pattern sweep of a module on
  the console, which would unlock porting by signature, the 1,100 refused `mask` patch lines, and
  finding an address for a cheat written from scratch.

### Also

* the titles cache cannot lose an invalidation: `console_titles_cached` releases its lock to read
  app.db (correctly - the read is megabytes) and used to store the result unconditionally on the way
  out, silently undoing any `titles_cache_drop()` that landed in that window. A generation counter
  now makes a drop win. And the autoarm reconcile drops the cache itself immediately before deciding,
  because the comment claiming the two-second wait rebuilt it could not be true against a five-second
  TTL.
* `cheat_doctor` asked the PC for `/api/mem/read`, which only the consoles have, and reported every
  entry "unreadable" - a broken tool that read as a broken engine.
* the mods reply carries `master` / `master_removable` from **all four** places that answer it
  (`/api/cheat/list` and `/api/mods/<tid>`, on both payloads), because a field on one is a silent
  dead button on the other device.

---

## [3.79.0] - 2026-09-27 - "Two traps the audit found" `[VERIFIED]`

Both of these were turned up by a full read of the panel and the console tracker, after the 3.78.0
fixes were already in. Neither was the reported symptom; both could have produced it.

### An incomplete exe was sitting in the repo under the real name

    pkg-mutant-shop/dist/PKG-MUTANT-SHOP.exe   21.9 MB   built 2026-09-23
        ps4-elf     ABSENT  -> a PS4 is never started, so it reads Offline for ever
        cheats-pack ABSENT  -> a PS4 has no cheat library at all

A build from an older layout, before PyInstaller was pointed at `companion/dist`. It looks exactly as
legitimate as the real one in a folder listing, and copying it to another PC produces **precisely the
symptom the owner reported** - "the exe seems to not have everything in it" - with nothing on screen to
explain it. They had not in fact copied it, but it was there waiting.

Renamed rather than deleted (the bytes are kept and that version cannot be rebuilt), with a
README-DO-NOT-COPY.txt beside it. New gate `tools/check_stale_exe.py`, in `ready_check`: any file named
`PKG-MUTANT-SHOP.exe` in the repo must carry all four things the spec bundles. Dated backups are
skipped - a backup is supposed to hold whatever it held.

### A PC that had never met a platform would never go looking for one

`start_console_tracker` gated its /24 sweep on `bool(quiet)` - a CONFIGURED console that stopped
answering. So on a PC whose only entry is a PS5, and whose PS5 answers, `quiet` is empty for ever, the
sweep never runs, and a PS4 on the same network can never be adopted however long the app is left open.

That is the right rule for "a known console is missing" - one that is merely switched off must
not have this PC sweeping all day. It is the wrong rule for "a platform never seen before", which is a
question that has never been asked and whose answer changes the moment a PS4 is switched on. There is
now a second, much slower trigger for exactly that case, backing off 15 min -> 30 -> 60 while it keeps
finding nothing, and stopping entirely once a console of that platform is adopted.

---

## [3.78.0] - 2026-09-27 - "The panel describes the console it is aimed at" `[VERIFIED]`

Two faults reported from the owner's own fleet, and neither was what it first looked like.

### A PS4 shown as Offline on a second PC - and the exe was innocent

The owner copied the exe to their other PC, saw the PS4 offline there, and concluded the exe was
missing something. It was not. Both PCs ran byte-identical 3.78.0; the difference was one line of
saved state:

```
PC "PC-A"     ps4_ip 10.0.0.86   online true     entry HAS a console_id
PC "Casita"   ps4_ip 10.0.0.87   online FALSE    entry has NO console_id
```

The PS4 is at .86 and answers there. Casita was polling an address nothing lives at - and **could
never have recovered**, which is the real defect:

* **follow-by-id** needs the ENTRY to carry an id to match, and an id is only ever learned from a
  console you can already REACH. An entry whose address went stale before it ever answered can never
  learn one. Chicken and egg, and permanent.
* **the no-id fallback** then required the CANDIDATE to report no id either. Every console reports one
  now, so that branch could never fire again - the arrival of ids silently disabled the fallback that
  existed for exactly this case.
* **adoption** skipped it because a `ps4` entry already existed.

The guard that blocked it reasoned that a console we CAN name must be different from an entry we
cannot. **That does not follow**: the entry has no id to compare against, so the candidate's id says
nothing about the entry - only that it runs a build new enough to have one, which is now all of them.
What the guard really protects is a house with a second console of the same platform, and that is
still refused: this fires only when exactly ONE console of that platform is unaccounted for, never
when its id already belongs to another entry, never across platforms, and the move is announced.

`tools/test_console_tracker.py` is 14 cases now, including the owner's exact shape. The old guard case
was replaced rather than deleted, with the argument written down.

`tools/test_fleet_two_consoles.py` had to change too, and for an instructive reason: its SUITE 1 runs
against the LIVE network, and with following now enabled a "dead" PS5 entry correctly followed to the
owner's real PS5 sitting on that network. It was green only because the old guard refused. Its
configured console now carries an id nothing answers to, so the refusal happens for a deterministic
reason - which is that file's own rule: *"A test whose result depends on which console is powered is
not a test."*

### An update installed on the PS5 could not be installed on the PS4

Grounded is on both consoles; only the PS5 had update 01.14, the PS4 being on 01.00. With the PS4
selected, "Updates & Patches" showed that update as **Installed**, so there was no way to install it
onto the PS4. Confirmed from the live library:

```
installed_version (flat)   01.14        <- what the row was reading
console_state.ps5.version  01.14
console_state.ps4.version  01.00        <- the truth for the console that was selected
```

The header was right at the same moment ("PS4 (update waiting)"), which is the tell: the per-console
answer already existed and a handful of places were still reading the fleet one. `st` was already
`stateOfOn(g,_cid)`, so the status badge, the Install button's words and the add-on gate were correct -
this is a targeted change to the remaining fleet-wide reads, not a rewrite.

The update row is now driven by `pendingUpdates(g,_cid)` - **the same function the header badge uses** -
so the two can never disagree again. The fact sheet's VERSION, SIZE, INSTALLED TO and FORMAT pills, and
the console-only base row, all ask per console.

### The fix had the same bug inside it, found by the adversarial pass

`installedVersionOn()` ends in a fallback to the flat `g.installed_version`, which the companion sets
from the **first** console found holding the title - the PS5 on this fleet. So whenever
`console_state[ps4].version` is empty (that console's app.db has no APP_VER row, and some CUSA titles
are appended with no `app_ver` key at all) the accessor handed back the PS5's version and the badge said
"Installed" with the PS4 selected. **The original symptom, reproduced from inside the per-console path** -
so moving the panel onto that accessor alone would not have closed it.

The flat fields are now only consulted when they cannot be about another console - when the title is on
at most one. Otherwise the answer is "unknown", and unknown is the safe direction: `pendingUpdates()`
treats an unknown version as "every update is still outstanding", so the owner is offered the install
rather than having it hidden behind a claim about the wrong machine.

New gate: `tools/test_panel_console_state.py` (19 answers plus 6 panel call sites, in `ready_check` and
the PS4 build). It pulls the helpers out of the page and RUNS them against a two-console fleet - a grep
cannot tell you that picking the PS4 changes the answer. It also asserts what stays fleet-wide on
purpose: `stateOf()` is the GRID's answer and is deliberately unchanged.

---

## [3.76.0] - 2026-09-27 - "The engine switches itself on" `[VERIFIED]`

Two pieces of friction the owner should never have met: they had to press a button to arm the helper, then
close and reopen the game, then refresh the app before the tiles would come alive. All three are gone.

### The helper arms itself, for games that have something, and only those

GoldHEN reads `plugins.ini` only when a game **starts**, so the decision has to be made before the launch
- which is exactly why a button could never be enough. It is also a decision we can make exactly: we know
what is installed and we know what the library covers.

Measured on this console: **14 installed titles, 5 with cheats.** So the payload now writes a `[TID]`
section for those and nothing else, and a game with nothing **never loads our module at all** - which is
strictly better than the blanket `[default]` form in every way. `[default]` is passed through untouched;
autoarm never reaches for it.

Verified on the live console at boot:

```
autoarm true   armed_titles 4   armed_default false
armed_for  CUSA01589,CUSA14409,CUSA20499,CUSA42556
```

**Four, not five** - because one installed title is on a skip list seeded on first run. Injecting
anything into a game with online services is a risk the owner of a console should choose for
themselves, not inherit from us. The list is a plain file, it says why, and it is theirs to edit.

**The purge is still the recovery path, one file away.** With autoarm off, boot behaves exactly as it
always did - so if an agent build ever misbehaves again, the owner opens the app, switches it off, and
every game is clean on the next boot with no re-jailbreak. Verified both directions: off purged 2 items
and reported `installed:false armed_titles:0`; on restored all four.

**It does not churn the owner's file.** The whole result is compared byte for byte with what was read and
written only if it differs - confirmed on the console, where a second reconcile left `plugins.ini`
byte-identical (827 bytes, same sha). Also re-armed automatically after a cheat intake, since a title that
had nothing a moment ago may have something now.

### The Cheats panel notices the engine by itself

`refreshModsQuiet()` already re-applied every tile from fresh state - and nothing ever called it on a
timer, so a panel opened before the agent was serving stayed grey until the app was reloaded by hand. It
polls every 3 s while the panel is on screen (`?state=1` measures 61 ms), and when `running` **flips** the
whole section is rebuilt in place rather than patched - because the "helper is not in this game" note and
the arm button are separate rows, and leaving them beside tiles that have just come alive is worse than a
redraw. 3 s also sits inside the agent's six-second busy window, so a panel somebody is looking at keeps
the engine at its fast poll rate for free.

### Two bugs the harness caught, one of them its own

`agent_autoarm_apply` rewrites the owner's plugin list on every boot, so it shipped with tests (the
`test_gh_ini.py` suite is now 90 checks). The idempotence check failed, for two reasons:

* **the harness was lying.** On Windows `open()` without `O_BINARY` translates `\n` to `\r\n` on write and
  back on read. The PS4 does nothing of the kind, so every pass added a `\r` to every copied line, the
  file grew, and a perfectly idempotent writer looked broken by an artifact the console cannot produce.
  The harness forces binary mode now, which makes it faithful rather than merely green.
* **a real one, and the same bug as two changes ago:** the line autoarm *emits* had a bare `\n` while the
  file it edits may use CRLF. On a `plugins.ini` the owner had edited on a PC, our inserted line would
  have been the one odd line out - and it would then compare unequal on every single boot and be
  rewritten for ever, which is the exact churn the purge gate was fixed to avoid. The document's own
  ending is detected once and used for every line we write.

---

## [3.74.0] - 2026-09-27 - "One message, and one signal to trust" `[VERIFIED]`

The engine's own message appeared **twice** at game start. One announcement per game now, and the fix is
about picking the right signal rather than adding a flag.

**What was wrong.** "Allow another announcement" was keyed on the running TITLE going empty:

```c
if (!cur[0]) announced_for[0] = 0;      /* wrong */
```

The running title is not steady during a game LAUNCH. `rt_refresh_once` needs
`sceSystemServiceGetAppIdOfBigApp` to report an id AND a process walk to find a process carrying it, and
across a long load those two briefly do not agree - so the title reads empty for a poll or two, the flag
cleared, and the message went out again.

**Why it had to be reasoned about, not reproduced.** Sampling the running title 20 times at rest showed
**zero** empty readings. The flicker only exists inside the loading window, which had already passed by
the time anything could be measured. Every notification site in the payload was listed first to confirm
only one of them could be responsible.

**The signal that is honest here is the agent's own heartbeat.** `alive.bin` is rewritten every two
seconds by the agent itself and is stale only when there is no agent - which is exactly the condition
that should permit a fresh announcement. It says nothing about the shell, the foreground app or a process
walk. Two consecutive misses are required, so one slow beat cannot cause a repeat either, and a change of
title still announces immediately because that really is a different game.

## [3.73.0] - 2026-09-27 - "Fifteen times faster, measured" `[VERIFIED]`

The cheat engine was correct but slow, and this is what it cost - measured on the console with Dark Souls
II running and `rc == 0` required on every one of 30 samples:

```
                        before            after
one agent round trip    495 ms avg        33 ms avg     15x
/api/cheat/running      501 ms            39 ms         12.9x
/api/mods/<TID>        1003 ms            61 ms         16.6x
a cheat toggle         1490 ms            85 ms         17.5x
```

Every panel action is a whole number of agent round trips, and each round trip was costing almost exactly
the agent's fixed 500 ms poll. Nothing else in the path was slow - the shop answers `/api/health` in
17 ms - so that one interval was the entire latency budget.

**The agent now polls at 25 ms while a session is in progress and 500 ms when idle**, with any request
keeping the fast window alive for six seconds (the panel's own polling sustains it for as long as somebody
is actually cheating). Polling fast all the time would have been the wrong fix: that loop lives inside
somebody's game for as long as they play, and 40 file checks a second forever to answer a question nobody
is asking is a real cost. **A game nobody is cheating in now costs less than it did before.**

Two things that had to move in the same change:

* **the heartbeat became time-based.** It was written every 4th iteration, which was every 2 s at a fixed
  500 ms poll - but at 25 ms that is ten file writes a second inside the game, far worse than the latency
  it buys. It now goes out at most every 2 s of accumulated sleep whatever the poll rate is, with the
  elapsed time accumulated from the sleeps themselves so it needs no clock and therefore no new import.
* **the shop checks for the answer every 10 ms** instead of 50 while a request is outstanding, because
  otherwise its own granularity would have become the new floor and thrown away half the gain.

**A measurement that was wrong, and how it was caught.** An earlier pass reported `/api/mem/read` at
10 ms and nearly recorded that as excellent performance. It was a **fast failure**: the shop
short-circuits on a stale heartbeat rather than waiting out the timeout, and the game had been closed.
Two separate tests in that round also measured nothing useful - one never checked `rc`, and another asked
for 65,536 bytes from a route that clamps to 256, so "16 chunk round trips" was one round trip every time.
Every timing here validates the reply before counting it. `AGENT_ALIVE_MAX_AGE` is now one constant shared
by both readers of that heartbeat instead of the same `8` written twice.

---

## [3.72.0] - 2026-09-27 - "Our voice, not GoldHEN's" `[VERIFIED]`

The owner noticed that the banner at game start said **"Loaded 1 plugin(s) 1. pms_agent"** in GoldHEN's
gold styling, and asked the right question: is the engine really ours, or are we running on GoldHEN?

### The honest answer, written down because it should not have to be asked twice

**Ours:** the whole engine - reading a cheat file in any of three formats, resolving an address against
the live image base, gating every write on the bytes the file documents, applying and reverting, the
panel, the library, the shop, the install lane. And the module itself: our source, our own crt
(`crt_prx.c`), our build recipe, our signing, our pinned 16-symbol import set. It links **nothing** of
GoldHEN's - not its SDK, not its hook library, not its syscall-500 gateway.

**Not ours:** the **injection**. GoldHEN's `plugin_loader` reads `/data/GoldHEN/plugins.ini` when a game
starts and `dlsym`s `plugin_load`. Carrying a module across a process boundary on a PS4 is a
jailbreak-level service: it needs kernel reach into the loader path, and this payload has **measured
EPERM** on both `mdbg(573)` and `ptrace(26)` - which is exactly why the engine runs inside the game
instead of outside it. That cannot be replaced without becoming a jailbreak, which GoldHEN already is.
The PS5 needs no equivalent only because its SDK hands us `kernel_copyin`/`kernel_copyout`.

**The banner, though, was ours to take.** It came from GoldHEN's own `show_load_notification` setting,
in a file we already write, describing our plugin in its voice, at a moment that is not even useful.

### So the visible engine is ours now

* the module calls itself **"PKG MUTANT SHOP Cheat Engine"** instead of `pms_agent`, so anything on
  GoldHEN's side that names a plugin names ours properly;
* `show_load_notification` is switched **off while our helper is armed** and **restored on purge** -
  it is the owner's setting and it governs every plugin they run, so we hold it down only while we have
  something listed. Verified live: the boot purge put it back to `true`, arming set it to `false`;
* the **shop** announces the engine itself, through the notification path it already uses for cheat
  toasts: *"PKG MUTANT SHOP: the cheat engine is on for this game. Switch cheats on and off from the
  app."* It fires when `alive.bin` shows the agent actually **serving**, not when the module is merely
  mapped - the agent waits six seconds before touching anything, so GoldHEN's banner was announcing
  readiness that did not exist yet. And it adds **no import** to the in-game module, which is the one
  place where a new import is still a risk worth avoiding.

### Two bugs the host harness caught before the console did

`gh_set_load_notification()` edits one key in the owner's config, so it shipped with tests - and they
failed immediately:

* it decided "does the file already say what I want" by comparing the **line's length** against the
  length the wanted line would be. With a trailing CR, `show_load_notification=true` and
  `...=false` are both 28 bytes, so it concluded nothing needed changing, wrote nothing, and
  **returned success**. A length is not a value. It now builds the whole document and compares it byte
  for byte with what it read - which is exact, and still avoids rewriting a file that does not change.
* the rewritten line **dropped the line's trailing CR**. `plugins.ini` is the owner's file and may well
  have been edited on a PC, so a CRLF config would have come back with one mixed line in the middle.
  The ending is preserved now, and the test suite carries a CRLF case because that is what exposed it.

`tools/test_gh_ini.py` is up to 68 checks.

### A measurement that was wrong, corrected

While looking for cheat-engine latency this pass reported `/api/mem/read` at **10 ms**. That was a
**fast failure, not a fast success**: the shop short-circuits on a stale `alive.bin` heartbeat rather
than waiting out the timeout, and the game had already been closed. Real round-trip numbers still need a
running game and are not claimed here. `AGENT_ALIVE_MAX_AGE` is now one constant shared by both readers
of that heartbeat, instead of the same `8` written twice.

---

## [3.71.0] - 2026-09-26 - "Three symbols" `[VERIFIED ON CONSOLE]`

**PS4 cheats now activate and deactivate in a running game, from the app.** Verified end to end on the
owner's console, and then CONFIRMED ON SCREEN: Dark Souls II running, the helper inside it, "Max Souls"
switched on from the PC, the patched bytes read back out of live memory, and the owner's soul count
jumping to **999,999,999** after killing one enemy - the same 0x3B9AC9FF the disassembly around the
patch site carries twice. The toggle was also driven off and on again and the game was left byte-for-byte
as it was found before the final test.

The cheat is a CODE PATCH, not a value poke, and that distinction is worth keeping: it rewrites
`mov [rdi+0xEC], ecx` into `mov [rdi+0xEC], eax` so the game stores the cap instead of the real value the
next time it writes your souls. Nothing visible happens until that instruction runs, which briefly looked
like a failure ("the message showed but the souls are still 0") when it was the engine working correctly
and waiting for the game.

### The cause, after weeks of wrong answers

Every edition of the in-game helper had crashed games - sometimes badly enough to need a reboot and a
re-jailbreak. The build recipe, the signing, the crt, the module param, the SDK version, the DYNAMIC
table, the export list, the `.bss`, the socket, SIGPIPE and `printf` had all been suspected, checked
and cleared, and the honest position at 3.70.0 was that the cause was unknown.

Two builds settled it, on one game launch each:

* **`PMS_AGENT_NULL`** - `plugin_load` sets a global and returns, **zero undefined symbols**. Dark
  Souls II **loaded and played**, and GoldHEN announced the plugin on screen. So the module format,
  the signing, our own crt, the paid, the SDK version and the whole build recipe are **fine** - and
  nothing from GoldHEN's SDK is needed, which was the one thing the owner had ruled out.
* **`PMS_AGENT_MINIMAL`** - the same build plus `fopen`/`fwrite`/`fclose`, called from `plugin_load`.
  **Crashed.**

Three symbols apart. **libc stdio is what breaks a module inside a game**, whether it is called on the
load path or from a worker thread six seconds later (the file-channel agent did the latter and crashed
too).

What had misled the whole investigation: `game_patch` - a plugin this console loads cleanly, which does
real file work - imports `fprintf`, `getc`, `putc` and the `stderr` FILE object. That reads like "the
FILE machinery resolves inside a game". It does not follow that `fopen` is safe to call, and
`game_patch` never calls it: it uses `sceKernelOpen`/`Read`/`Write`/`Close`/`Lseek`/`Mkdir`. Two
different claims, and only the first was ever evidenced.

### The fix

Every file operation in the agent is now a `sceKernel*` call - the exact set `game_patch` proves. Its
import list went from 18 names to 16, with all stdio gone, and each signature was read out of the
toolchain's own `libkernel.h` rather than inferred.

`rename` went too. It was the one import nothing on this console demonstrated, and it only existed so
a reader could not see a half-written file - which the protocol already guarantees better: a response
carries its own length, so an incomplete one is an error and never a value. `sceKernelUnlink` is
deliberately not used either, although it exists; a file is emptied with `O_TRUNC`.

Three things that fell out of dropping the atomic rename, all of which would have been maddening to
diagnose on a console:

* both readers could now catch a reply **mid-write**. They wait for the declared length instead of
  calling it malformed, and neither deletes the file before it parses - the old code deleted the
  agent's in-progress answer.
* `cmd.bin` is emptied rather than deleted, so the shop's `rename` onto it became a rename onto an
  **existing** file - which is exactly what fails on these consoles. It clears the target first.
* the PC harness's own `sceKernelOpen` mock translated FreeBSD flags, but on a PC `main.c` is compiled
  against glibc's headers, where `O_TRUNC` (01000) is numerically FreeBSD's `O_CREAT`. `O_TRUNC` was
  silently dropped, `cmd.bin` was never emptied, the agent re-served every request, and eight checks
  failed in a set that moved between runs - none of them pointing at the agent. **The mock was the
  bug.** It passes the flags straight through now, because in that build the producer and the consumer
  share one header.

### The switch is in the app now

`agent_enable_for_title()` had existed on the console for a while with **no caller anywhere** in `web/`
or `companion/` - the only control was the Settings row, which arms the helper in *every* game, and
3.70.0 started refusing that unless it is asked for by name. So the app had no working way to switch
the helper on at all. The Cheats panel now carries **"Turn the in-game helper on for this game"**,
which appends a `[TID]` section and touches no other game and no other plugin the owner runs. The
Settings row keeps the console-wide form and now says `all_games` out loud. The companion refuses a
request with no `enabled` field rather than turning it into a purge.

### Measured, for the record

```
agent_present true   pid 119   base 0x400000   module eboot.bin   title CUSA01589
can_cheat     true   helper true   titles_agree true
0x400000  2F6C6962657865632F6C642D656C662E  -> "/libexec/ld-elf.so.1"
0x803B9E  898FEC000000   the cheat file's documented OFF bytes
toggle ON   ok:true  entries=1 written=1 skipped=0 failed=0   -> 8987EC000000
toggle OFF  ok:true  entries=1 written=1 skipped=0 failed=0   -> 898FEC000000
```

The agent survives a payload reload inside a session that is already running - the boot purge deletes
the `.prx` and unlists it, which does not unload a module already mapped into a live process. So
updating the shop mid-game does not interrupt cheats; only the next game launch needs the helper
switched on again. Verified.

The PC suite is 30/30 across five consecutive runs, driving the real agent against the real shop
client, and it now fails the build if any stdio call reappears - with a positive control proving the
pattern catches stdio and leaves the `sceKernel*` replacements alone.

---

## [3.70.0] - 2026-09-26 - "Forty-six findings, and a gate that could not fail" `[VERIFIED]`

A full audit of the PS4 cheat work. Everything below is fixed, and the two things it got wrong
about its own headline finding are recorded as plainly as the things it got right.

### The gate that had been checking nothing

`tools/test_agent_protocol.py` carries a static gate over the in-game module: no signals, no raw
POSIX file calls, no `printf` that could block inside somebody's game. Both of its patterns began
with a **literal ASCII backspace byte** where a word-boundary `\b` was meant. `main.c` contains no
such byte, so every `findall()` returned an empty list and the suite printed "26 check(s), 0
failure(s)" for a check that **could not fail**. It now reads a comment- and string-stripped copy of
the source in one alternating pass (so the project's own prose *about* `signal()` and `stat()` does
not count as calls), allows the `fwrite` the file channel is built on, and **runs both patterns
against a known-bad snippet and fails if they do not trip**. A gate that cannot fail is not a gate -
which is the whole lesson, and it is now enforced by the file itself.

### The diagnostic that was about to ship

`ps4-app/onconsole/agent_bundle.h` `.incbin`s `build/pms-agent.prx` by that exact path, and the PS4
ELF embeds whatever is there - and the exe ships that ELF. A `PMS_AGENT_MINIMAL=1` build wrote to that
filename, so every PS4 ELF built afterwards silently carried a diagnostic probe instead of the agent.
(The audit said *both* console ELFs embed it; only `server_ps4.c` includes `agent_bundle.h`.) Each variant now owns
its own basename (`pms-agent-minimal.prx`, `pms-agent-nulltest.prx`) and cannot touch the shipping
name.

### The owner's plugin list

`/data/GoldHEN/plugins.ini` is **their** file - every other plugin they run is listed in it, and it
survives a reboot and a re-jailbreak. Six defects at once, all of them found by lifting the real
functions out of the payload, compiling them on the PC and driving them against a temporary file
(`tools/test_gh_ini.py`, now a build gate and part of `ready_check`, 45 checks):

* **Every write failed.** `rename()` will not replace an existing file here - this repo already
  records that quirk in the agent's own `write_atomic` - so the new whole-file writer returned "could
  not be written" for every toggle. It now tries the replace first and only clears the way when the
  platform refuses, because unlinking first would leave the console with no plugin list at all for
  the width of that window.
* A **substring** match counted prose that merely mentions the file as a listing. It is a token match
  now: optional whitespace, our own `;` spelling, then the first token.
* The scan **stopped at the first mention**, on a comment claiming "we only ever write one" - which
  `agent_enable_for_title` falsifies by appending a second mention in a `[TID]` section. A per-title
  arm was invisible to the only instrument the project has for "is this module wired into a game".
  `/api/engine/agent` now reports `armed_default`, `armed_titles` and `armed_for`.
* The branch that inserts our line **inside** an existing `[default]` was unreachable (`!X && X`), so
  arming appended a **second** `[default]` at the end of the file. Two passes now.
* Both writers copied each line through `char[512]` and then emitted the **clamped** copy, so any
  line of 512 bytes or more in the owner's config permanently lost its tail. The original span is
  emitted now; the bounded copy survives for the tests only.
* `agent_purge` re-entered the writer whenever a line was merely *present*, so every boot rewrote the
  file to byte-identical content while klog announced a removal that had not happened.

### Never arm every game by accident

`POST /api/engine/agent` defaulted `enabled` to 1, so a **bodyless POST, an empty `{}`, or a
misspelled `{"enable":0}`** switched the helper on inside every game. The key is mandatory now, via a
sentinel - moving the default to 0 would have turned the same malformed request into a silent purge -
and the every-game branch additionally requires `all_games:1`, with its own sentence rather than a
false "the plugin list could not be written".

### Writing into the wrong game

* `/api/cheat/running` is the route the panel decides `can_cheat` from. It took the **title from the
  console** and the **pid and base from the agent** and never asked whether they describe the same
  game - so an agent still answering from a game the owner had just closed reported the *new* title as
  ready to cheat with the *old* game's pid. `running_game` already refused this; the route that gates
  the button did not. The compare is written once now and enforced in both places.
* Two routes reached for the PS4's no-ASLR load address whenever `base` was absent, turning "I do not
  know this game's base" into "this game is at 0x400000". They ask the agent first.
* A **write** is now allowed only inside `eboot.bin`; reads still reach any module. Every offset in the
  library is relative to the executable, and `eboot.bin` is the one module that cannot be unloaded
  while the process lives, which takes the stale-map fault off the write path. Refused with its own
  status, and covered by the harness.
* The page protection is **put back** after a patch. Guarded, because a protection that came back 0
  must not be restored - handing a live code page `VM_PROT_NONE` would fault the game.
* `/api/mem/write` - a GET, with everything in the query string - was the one state-changing route on
  this console **outside** the cross-site guard. The PS5 had always guarded its equivalent.
* The file channel is **serialized**. A thread per connection and no lock meant two requests shared a
  seq counter, two static buffers and one pair of files on disk; one could read the other's reply and
  act on it, and acting on it means writing bytes into a running game. A short bounded wait, then
  "busy" - never a queue on a route the panel polls.

### The version picker was a dead control

* Clicking a version **changed nothing on screen**. `cheatSection()` builds a detached node and the
  click handler threw the result away. It replaces the node in place now.
* A version string is a **filename key**, and the longest in the shipped library is 29 characters
  (`01.03_ac3_engine_orbis_fn.elf`). Every buffer that parsed `?version=` was 16 or 24 bytes, so those
  versions were clipped and resolved to no file at all. All of them are 48 now, with a check that
  keeps it that way. The label shows the numeric head with the rest as a dim qualifier; `data-ver`
  still carries the whole string, because the tails are different executables of the same disc.
* The picked version now **travels with the write**. A mod is applied by index, so listing one
  version's cheats while the console resolves another's applies a different cheat and reports success
  under the wrong name. Both consoles refuse a version they cannot match exactly.
* `compatible` was computed partly from "exact against what was **asked for**", so deliberately
  picking a non-installed version painted the row green and hid the mismatch note. It is about what is
  installed, on both the console and the companion.
* The override was keyed by title alone, so switching console tabs carried it into the other
  console's library, and it could never be cleared for a title with no readable installed version.

### Things that were simply not true

* Three comments and one owner-facing message said the exe bundles only `web/` and therefore "has
  nothing to push - by design". The spec has **four** datas entries, one of them the cheat library,
  which the companion expands beside the exe at startup. The PS4 depends on it.
* A branch chose a string saying cheats need a PS5. Nothing emits the value it tested, and the claim
  has been false since PS4 cheats shipped. Deleted, and the string it selected was rewritten in all
  15 languages; a second copy of it was dead weight and is gone.
* "Start the game first", shown to somebody looking at that game on their television, because
  `running` means *can_cheat* on a PS4. The note now mirrors the tooltip.
* `/api/cheat/library` asserted `"status":"ready"` as a literal, so an entirely empty library reported
  the same status as a full one.
* `/api/cheat/find` and `/api/patch/list` echoed the requested version back as `installed_version` -
  the request returned as a fact about the console.
* A **stale** cheat archive shipped silently. PyInstaller fails loudly on a missing data file and says
  nothing about an out-of-date one; the spec now repacks and then insists.

### Smaller, but real

* A game patch XML dropped on the console was filed as a **cheat**. All 376 patch documents are XML,
  so the generic `<?xml` fallback claimed them as `shn` - which removed them from the patch directory
  and, because a generic file outranks an other-version match, made them outrank the real versioned
  trainer for 200 titles. `cheat_sniff` tests for `<Patch`/`<TitleID>` first (measured: **0** of the
  5,193 cheat files contain either), and a patch is filed under **every** `<ID>` it covers - 303 of
  376 cover more than one title, and the filename matches the first id in only 156.
* Intake now descends one level into a folder named like the library's own, so a USB stick holding a
  copy of it files something instead of reporting "filed 0".
* A live `.part` temp sat inside the four folders the cheat sync pushes from, so it could be sent -
  and it is written as a dotfile now, with `.part` excluded at the listing as well.
* The agent's `.bss` went from **138,848 bytes to 16,640**: two 64 KiB scratch buffers on a path that
  never moves more than 256 bytes, reserved inside somebody's game. The build prints `.bss` and every
  `PT_LOAD` pair now, and refuses to exceed a ceiling.
* The worker no longer exits silently and permanently when it cannot find a writable directory - the
  commonest reason is that the game started before the payload created it. It keeps looking, and
  leaves one mark the first time, in the shipping build, with no new import.
* `sceKernelGetAppInfo` is off the game's load path. `plugin_load` is now `scePthreadCreate`,
  `scePthreadDetach`, `return 0` and nothing else.
* `\n` appeared as two literal characters mid-line in **both** console build scripts, so a gate ran as
  `test_console_tracker.py n`.
* An engine refusal reached the owner as `entries=3 written=0 skipped=0 failed=3`. There is a sentence
  now, written once in the shared engine and carried to the PS4 by the sync tool - and it says
  **partly applied** when that is what happened, because the engine carries on past a failed entry.
* A refused or timed-out toggle repainted from the state it was drawn with, so a mod with real bytes
  in the running game showed as cleanly off. Both arms re-read the console.

### What the audit got wrong, recorded because it matters more than what it got right

Its headline finding was that the module's `.bss` made the loaded memory image exceed the file image,
correlated across 13 modules. **The minimal build falsifies it**: measured, `memsz == filesz` in every
`PT_LOAD` of that build, it imports only `fopen`/`fwrite`/`fclose`, and it still broke the game. The
buffers were shrunk anyway, because 128 KiB inside somebody's game for a 256-byte path is
indefensible - but not as a fix, and the code says so.

Its second finding was that a note in this project claiming "no breadcrumb means the failure is at or
before module load" is a **non-sequitur**, and that is correct. A failed `fopen` inside the game's
sandbox produces the same empty result, and so does a fault anywhere inside `plugin_load`. That claim
has been struck from the source and from the project's memory.

What the minimal build **does** establish is narrower and still useful: with no thread, no segment
scan, no protocol, no `.bss` and three stdio imports, the game still broke. None of our own logic can
be the cause. A `PMS_AGENT_NULL=1` build now exists that imports **nothing at all** - verified, its
dynamic symbol table has zero undefined entries - which is the cheapest remaining question and the
last one that can be asked without touching the signing plumbing.

The in-game helper remains **disarmed** on the console, as it has been since it first broke a game.
Cheat browsing, the version picker, patches and the whole library work on the PS4 today; only writing
into a running game waits on that question.

---

## [3.68.0] - 2026-09-26 - "Cheats are a PS4 feature now" `[VERIFIED]`

The PS4 cheat engine was finished. Not the engine itself - that part was already right, and is
literally the PS5's, copied by `tools/ps4_sync_cheat_core.py` - but everything around it, which
was still written on the assumption that mods were something only a PS5 could do.

### The in-game helper, and the test that proves it without a console

A PS4 cannot reach a running game's memory from outside. `mdbg` (syscall 573) returns **EPERM** and
`ptrace` returns **EPERM before it even looks the target up** - both measured from this payload on
13.52, read-only probes, twice. So the engine runs **inside** the game: `pms-agent.prx`, a small
GoldHEN plugin that answers four questions on `127.0.0.1:9231` - are you there, what process is
this, read these bytes, write these bytes - and knows nothing about cheats, files or titles.

Its first version **crashed games**, and the cause is worth naming because it is invisible: a write
to a socket the shop had already closed raised **SIGPIPE, whose default action terminates the
process**, and the process was somebody's game. No message, no log line, just a crash after a while
of play. The same build also made every memory read fail, because the shop put the length of its
outgoing payload in the request header - which for a read is zero, and the agent refuses a zero
length. From the panel that is indistinguishable from "no game is running".

Neither fault needed a PS4 to find. Both needed a test, and now there is one.
**`tools/test_agent_protocol.py` compiles the actual shipped plugin** (against
`ps4-app/plugin/test/stubinc`, whose declarations are copied from the toolchain's own headers)
**and the actual client lifted out of `server_ps4.c`, and runs them against each other** on the PC.
The fake game is one mmap'd megabyte carved into three module segments with real gaps between them,
so the refusal checks mean something. 31 checks, including the two faults above reproduced
directly: the SIGPIPE disposition is *queried* after `plugin_load` rather than set, and a client
that rips its connection down mid-answer must leave the agent alive. `ready_check` gates it.

**The agent also keeps up with the game now.** Its module map was built at `plugin_load`, which is
the earliest moment of a game's life, so an address in a library the game loaded seconds later was
refused as "not in any module segment" - correctly, by a map that was simply out of date, and it
read as a bad cheat file. A miss now costs one syscall (`sceKernelGetModuleList` reports how many
modules there are without asking about each one) and rebuilds only when that count has changed.
The map is rebuilt and never added to, because a segment belonging to an unloaded module would let
a read through to memory that is gone - and that fault is fatal inside the game.

### A PS4 game's toggles were being sent to the PS5

One line. `modsConsole()` returned `""` for a PS4 so the PC would resolve the request to a console
that could actually serve it. That was right when the PS4 payload answered every cheat route with
"not available on the PS4 yet"; once the PS4 had an engine it meant **the console the request was
about was the one id the request was forbidden to carry**.

Alongside it: the companion had no `/api/engine/agent` relay at all, so the helper's on/off control
did nothing from a PC; `/api/cheats/paths` refused on `is_ps4()` before asking the console; and
Settings hid the whole Cheats card behind "mods run on a PS5 today". All three described a console
that stopped existing in this release.

### "The game is running" and "a cheat can be written into it" are two questions

On a PS5 they are one - the payload reaches game memory itself. On a PS4 they come apart, and it is
the ordinary case rather than an edge one: a game started before the helper was listed is running,
has its cheat file, and cannot be written to. The companion read `running` from title + cheat file
alone, so every tile was offered as pressable, the write went to pid 0, and the panel reported a
failure that reads as a bad cheat file. It now reads the console's own `can_cheat`, and passes
`game_running` and `helper` through so the tile can say the true thing: **close the game and open
it again**. The console's own refusal tells the two causes apart the same way.

### Game patches could never be reverted on a PS4

The shared patch engine saved the original bytes of every line it overwrote with `fopen`/`fwrite`.
**`fopen` does not work in the PS4 payload** - every other writer in `server_ps4.c` uses `open()`
for exactly this reason - so it returned NULL, the patch applied perfectly, and Revert answered "no
saved original bytes" for ever. The record is written with a file descriptor now, one `write()` per
line rather than three, and a short write abandons the record instead of leaving half of one:
reverting half a record restores some lines and then writes wrong bytes at the next offset, into a
running game.

### The running game goes to the top of the library on both consoles

Two separate faults with one symptom.

**The PS4's watcher accepted NPXS ids.** Six lines above it a comment says the system processes
"must never be reported as the running game"; the code said `CUSA` or `NPXS`. NPXS is exactly what
it must not accept - the shell, the store and **the browser** are all of them - so the moment the
owner opened this page on the console itself, the "running game" became a system app, matching no
card. `CUSA` or `PPSA`, like the PS5.

**The PS5's own `/api/health` never carried `running_title` at all**, so a page served by the PS5
never floated anything either. It does now, for two library calls behind a three-second memo - the
title needs no process walk, only the pid does.

**And the PC asks every console, not just one.** `/api/health` filled `running_title` from the
console the request was "about", which on a PC with no `?console=` is the first one configured. A
game running on the other console was never asked after.

### The PS4 can take delivery of a cheat file

It had no intake at all: the Rescan button, the FTP drop folder that `/api/cheat/paths` advertises
by name, a USB stick with a `/cheats` folder - all PS5-only, and the PS4 answered the rescan route
with "not implemented yet". The intake moves into the shared engine (one implementation, both
consoles): it decides what a file **is** by looking inside it, so a `.mc4` is only believed once it
decrypts to Trainer XML, and it copies rather than moves from a USB stick because that stick is the
owner's. The library directories are created at boot, so a console with no PC has somewhere to put
a cheat.

**Verified on the console:** a real cheat file written into the inbox as `PMSTEST99999_01.00.txt`
came back filed at `.../cheats/json/PMSTEST99999_01.00.json`, byte-identical, and immediately
findable by `/api/cheat/find`.

### The shipped exe carries the cheat library

A PS5 is self-sufficient - its ELF embeds all 7022 files. A PS4 cannot be: its payload lives in a
shared system daemon whose heap it does not own. So it takes the library from the PC, and **the
shipped exe had none to give** - `CHEATS_DIR` pointed into the frozen bundle at a folder nothing
put there, so cheats on a PS4 were a repo-only feature. The exe now carries the library as one
10 MB archive (`tools/pack_cheats.py`, 6275 files, 26.5 MB of JSON and XML) and expands it beside
its own `config.json` exactly once, keyed on the archive's identity. One archive and not 6275 loose
files because the one-file build unpacks every bundled data file into `%TEMP%` on every launch.
The exe goes from 29.5 MB to 39.0 MB.

### The rest of the PS4's cheat surface

* **`/api/mem/write`**, with the same expect-gate and `expect_mismatch` reply as the PS5. Read-only
  was right while nothing could write; the agent writes now.
* **The patch route asked "is the game running" before "does this title have a patch file"**, so a
  title with no patches was told to launch a game. And it never checked `AppVer`, so a patch written
  for another build was applied to whatever was running - the one thing the PS5 refuses outright.
  It now answers with `title_id`, `name`, `app_ver`, `installed_version`, `partial` and a message,
  and raises the same toast the PS5 does.
* **`/api/cheat/find` answered in a different shape** - `reason` where the PS5 says `match`, and no
  `exact` - and the companion reads both by name, so every PS4 answer read as "no reason given".
* **`/api/patch/list` claimed `ok:true` with no patch file**, ignored the `&version=` the companion
  sends, and omitted `installed_version` and `file`.
* **`running_game()` trusted two sources that can disagree.** The pid and base come from the agent;
  the title comes from the console. An agent still answering from a game the owner has left would
  hand back a live pid for a title that is no longer running. They are compared now, and a
  disagreement is a refusal rather than a guess about which one is stale.
* **`agent_deploy()` re-listed the plugin on every boot**, so an owner who deliberately turned the
  helper off got it back at the next payload reload. It only enables what it has just installed for
  the first time.
* **A POST to `/api/fs/delete` answered a silent `{}`** - every `/api/fs/` operation except write
  lives in the GET half. Third time this trap has been hit on this console (the queue buttons, the
  Save button), found while cleaning up after the intake test.
* The tile subline read `m.type` and `m.patches`, which **no payload and no companion route has
  ever sent**, so the second line of every mod tile has always been empty. It reads `entries` now,
  which both consoles do send.
* Rescan in Settings went out with no `?console=`, so on a fleet it filed onto whichever console
  the PC resolved first.

### There is no way to start a game from the payload, and that is now written down

A route to launch a game was written - so the last link in the chain could be proved with nobody in
the room - and then taken out again, because it cannot be done here. Measured with
`/api/engine/symprobe` against all five libraries the payload loads:

```
sceLncUtilLaunchApp              false   <- the only one with a real signature
sceSystemServiceLaunchApp        true    <- declared `void f()`: no arguments, no shape
sceSystemServiceKillApp          true    <- typed, but three of its four ints are unnamed
sceUserServiceGetForegroundUser  true
```

There is no `libSceLncUtil.sprx` in `/system/common/lib` (439 entries) or `/system/priv/lib` (23),
both listed from the console; its exports live inside ShellCore. Using the one that does resolve
would mean inventing its arguments, which is the thing that crashed a console here once. **So the
last step - a cheat landing in a running game - is verified by a person starting a game.**
Everything before it is verified here.

---

## [3.67.0] - 2026-09-25 - "It knows where its consoles are" `[VERIFIED]`

The PS4 icon stopped working and the app showed the console as offline. Two different faults, and
only one of them was ours - but the one that was ours had been waiting to happen on any network
with DHCP.

### The app could not follow a console that changed address

**Measured.** The PS4 was restarted, took a new DHCP lease and came back at `10.0.0.86`.
`config.json` still said `10.0.0.87`. Discovery found the console perfectly - `confirmed=True
platform=ps4` - and **nothing compared what it found against what was saved**, because an ADDRESS
was the only name this app had ever had for a console, and the address is exactly what changed. A
healthy console was reported offline indefinitely while it sat one number along, answering.

Three things were needed and they only work together:

**1. Identity.** Every console now writes itself a sixteen-character id the first time the payload
runs, keeps it in `/data/pkg-mutant-shop/console-id`, and reports it in `/api/health`. It survives
reboots, address changes and payload rebuilds. It is random and local - not a serial, not a MAC,
not an account - and deleting the file simply gets a new one. Both payloads carry the identical
function; the single line that differs is the clock helper, which the two files have always spelled
differently.

**2. Reconciliation.** `track_consoles()` is now the only writer of a console's address. Identity
first, and only then the fallback of "the one unclaimed console of that platform".

**3. Continuously.** A watcher checks the cheap thing often (one `/api/health` per console every
45 s) and the expensive thing rarely (a /24 sweep only after a console has actually gone quiet, and
at most once every three minutes). Doing this once at startup would have fixed today and missed
tomorrow, because a lease expires while the app is running.

**Its refusals are the point.** It will not move an entry onto a console whose id belongs to a
different entry, will not choose between two equally plausible candidates, and never renames or
removes anything. `tools/test_console_tracker.py` runs nine cases against a fake network - five of
them are refusals - and gates both ELF builds. Writing it caught a real bug immediately: calling
`reconcile_consoles()` after a correct move **emptied the console list**, because that function
folds the settings panel's address fields into the list and the test had none set.

### A PS4 that is switched on gets its shop back by itself

A GoldHEN payload lives in RAM. It does not survive a reboot, and GoldHEN has no autoload folder -
its config has a `[BinLoader]` switch and nothing that runs a payload at startup. So after every
restart *something* has to hand the ELF to the loader. The home-screen icon does that when pressed,
and now the PC does it when nobody presses anything: a console that is reachable but has no shop
gets the payload handed to it, once every five minutes at most. The exe carries the PS4 payload for
this (+7 MB).

**The POST is still the only thing that touches port 9090.** A connection that is accepted and left
empty is what stops that loader listening - this console has lost it that way twice - so the
shared `_port_open()` helper now carries that warning where anyone reaching for it will read it.

### The icon says which failure it hit

The owner saw a black screen and then "cannot start". That message was ours and it was accurate -
no payload loader answered - but it covers two situations that need different actions:

* the jailbreak is not loaded at all; or
* the jailbreak **is** loaded and only its payload loader is gone, which is the ordinary state
  after a rest/resume and is documented in `ps4-app/onconsole/README.md`: *"9090 does not survive
  rest mode: after a suspend/resume, FTP and klog come back and the payload loader does not"*.

One connect to FTP tells them apart, and the icon now names the one it hit. It also retries the
loader once after two seconds, because pressing the icon moments after the exploit lands can find
the loader still coming up.

### Verified

Both consoles on 3.67.0 reporting durable ids (`d125ea52…` and `25301f11…`), stable across reads
and across a payload reload. The tracker was run against a copy of the real config with the PS4
genuinely moved: it followed `10.0.0.87 -> 10.0.0.86` and took `ps4_ip` with it. The home-screen
package on the console is now byte-identical to the built one (`caf069e4…`, `stale:false`), and the
PS4 cheat engine still lists (`CUSA20499`, 5 mods).

**Still not verified: turning a cheat on inside a running game.** That is the next thing, and it
needs a game.

---

## [3.66.0] - 2026-09-24 - "The PS4 gets the engine" `[VERIFIED, except in-game]`

Cheats work on the PS4 now. Not GoldHEN's - ours, the same engine the PS5 has been running, reading
the same library and answering the same API. Getting there meant proving two things impossible
before finding the thing that was not.

### Both ways in from outside are shut, and that is measured, not assumed

3.65.0 established that `mdbg` (syscall 573) returns **EPERM** from our payload. This version asked
the only other question the PS4 toolchains on this machine document: `ptrace`. `/api/engine/ptraceprobe`
issues `PT_ATTACH` against a pid picked out of the live process list precisely so it does **not**
exist - nothing is attached to, no game is involved - and FreeBSD looks a process up *before* it
checks permission, so a reachable syscall would have answered `ESRCH`.

It answered **`EPERM`**, before it even looked.

Two independent userland routes to another process's memory, both refused at the credential check.
Not a missing call, not a wrong struct: our payload simply does not have the privilege, and under
GoldHEN there is no way to grant it. **Neither should ever be probed again** - that is recorded
where the next person will look.

### So the engine went inside the game, which is where GoldHEN's has always been

Read off the console rather than recalled: `/data/GoldHEN/config.ini` carries
`[PluginLoader] Game_Patch_Enabled = 1`, and `/data/GoldHEN/plugins/` holds `plugin_loader.prx` and
`game_patch.prx`. GoldHEN loads code **into the game process**, where patching memory needs no
permission at all because the memory belongs to the process doing the writing. Its plugins are
fake-signed with the ELF body in plaintext, so the contract was readable directly from the files
the console already had: `plugin_load` / `plugin_unload`, dlsym'd by the loader, driven by
`plugins.ini` with `[settings]`, `[default]` and `[<TitleID>]` sections.

**`ps4-app/plugin/pms-agent.prx`** is ours. It is deliberately a pair of hands and not a brain: it
knows nothing about cheats, files, formats or titles, and answers four questions - are you there,
what process is this, read these bytes, write these bytes. Everything else stays in the shop, where
it can be tested without a console. Four rules, each for a reason:

* **Nothing happens until it is asked.** `plugin_load` starts a listener and returns.
* **No address is touched without asking the kernel first.** `sceKernelVirtualQuery` on both ends of
  every range - only its *return code*, never its struct, whose fields the OpenOrbis header names
  `unk01`/`unk02`. A cheat with a bad offset gets an error instead of crashing the game, and a bad
  offset is the *normal* failure of a cheat file written for another build.
* **Loopback only.** Game memory is never exposed to the network.
* **Failure is silent.** If the socket will not bind it returns anyway and the game runs as if the
  plugin were not there. A cheat tool must never be why a game will not start.

It builds from the OpenOrbis toolchain alone - `crtlib.o`, `link.x`, `create-fself --lib` - with no
GoldHEN SDK dependency, which matters because that SDK would not clone on this machine. One trap
cost a build: **`--export-dynamic`**. Nothing inside the module references `plugin_load` (the only
caller is the loader, from outside, by dlsym), so the linker dropped it and produced a clean `.prx`
with an *empty* dynamic table - which loads without complaint and does nothing. The build now
refuses if either entry point is missing.

### The PS4 runs the PS5's cheat engine, not a second copy of it

`tools/ps4_sync_cheat_core.py` extracts 61 functions from `ps5-app/onconsole/server.c` into
`ps4-app/onconsole/cheat_core.h` - the AES for `.mc4`, the Trainer-XML converter for `.shn`, the
library picker, the document walker, the expect-gate, conflict detection, apply/revert and the
whole game-patch engine. None of it is platform-specific; it reaches memory only through
`mem_read`/`mem_write`, which each console defines for itself. **Both** ELF builds run it with
`--check`, so a change to the PS5 that the extractor can no longer find fails the PS5 build too -
the failure belongs where the edit was made.

Writing that extractor surfaced a real trap worth recording: counting `{` and `}` per line to find
a function's end is wrong, because `next_mod_block()` is *written in terms of* the characters `'{'`
and `'}'`. Its depth never returned to zero. Any function quoting a brace would have been truncated
the same way and, being merely short rather than absent, would have done it **silently**.

### Measured on the console, with the library synced to it

`/api/mods/CUSA14409` on the PS4: found `CUSA14409_01.04.json`, read the installed version `01.00`
out of app.db, and flagged `compatible: false, reason: "other version"` - the same judgement the
PS5 makes. All three formats parse there: JSON, a `.shn` converted from Trainer XML (4 mods), and an
AES-256-CBC `.mc4` decrypted (5 mods).

The cheat library sync no longer refuses a PS4. Its old reason was sound and is now obsolete, and
the background sync asks **every** console rather than a PS5 by name - the PS4 needs it more, since
the PS5's ELF ships the library inside it while the PS4's arrives only this way.

### The panel is one code path again

The PS4 branch added in 3.65.0 - borrow the PS5's list, show it read-only, explain why - is gone
along with the string it displayed and the inert tile renderer it used. Both tabs ask their own
console and render identically, and everything the panel already did about a game that is not
running applies to the PS4 for free. `platform === "ps4"` is no longer treated as a refusal.

### An off switch, because it loads into every game

`plugins.ini` puts the helper in `[default]`, so it loads into every game that starts.
`POST /api/engine/agent {"enabled":false}` comments that one line out and leaves every other line
of GoldHEN's config untouched - it may list plugins this app knows nothing about. Two of this
version's bugs were in that writer and both failed safe: `fopen()` does not work from this payload
(every other writer in the file uses `open()`/`write()` - now so does this one), and a headroom
check of 1024 bytes against a buffer only 512 larger than the file declared every real config
"truncated" and refused. Neither ever corrupted the file, which is what the refusal was for.

### What is NOT verified

The in-game half. Everything above was measured with no game running; the agent has never been
loaded into one, because the owner asked that the game side wait until they were home. The listing,
the formats, the version matching, the toggle's refusal path and the helper switch are all
confirmed. Turning a cheat **on** is not, and is the first thing to do next.

---

## [3.65.0] - 2026-09-24 - "What the PS4 actually said" `[VERIFIED]`

A claim this project had been repeating for three versions turned out to be wrong, and the owner
said so. Finding out exactly how wrong took two read-only probes on their console, and the answer
changed what several screens are allowed to say.

### "The PS4 gives our engine no way to reach a running game's memory" was not true

`server_ps4.c` has said since it was written that the cheat engine has no PS4 equivalent, because
GoldHEN provides no `kexec` and therefore no kernel read/write. The first half is correct and was
measured three separate times. The conclusion was not.

The PS5 engine *writes* memory through a page-table walk because that is what a PS5 needs - but it
*reads* through `mdbg_copyout`, and mdbg is not kernel work at all. It is syscall 573, a userland
call, declared and implemented by the SDK whose crt this payload already links, and already
compiled into `PKG-MUTANT-SHOP-PS4.elf`. What blocks it is narrower than "no way": the SDK's own
wrapper elevates the calling process to `SCE_AUTHID_COREDUMP` before calling, that elevation is
four `kernel_*` calls, and those need the kexec we do not have - so the wrapper returns -1 *before
ever issuing the syscall*.

So the syscall was issued directly, with the elevation skipped. `/api/engine/memprobe` reads
sixteen bytes of **our own process, into our own buffer** - no game involved, nothing written - and
reports the raw return rather than a verdict. On FW 13.52 the kernel answers **EPERM**. The door is
real; we do not have this particular key. That is a measured limit with a named cause, which is a
different thing from the impossibility the code claimed.

Nothing here was guessed. Syscall number, both operation codes and all three struct layouts were
read out of `crt/syscall.h` and `crt/mdbg.c`; the syscall stub is the SDK's own `__syscall`, copied
rather than rewritten, because inventing a calling convention is the class of guess that has cost
this project a console. `ps5debug-NG` on this machine issues the identical 573 with the identical
`{1, 0x12|0x13}`, which is as close to independent confirmation of the ABI as is available here.

### How GoldHEN does it, read off the owner's own console

Not from its source, which is not on this machine, and not from memory. From the files GoldHEN has
installed on the PS4: `/data/GoldHEN/config.ini` carries `[PluginLoader] Game_Patch_Enabled = 1`,
and `/data/GoldHEN/plugins/` holds `plugin_loader.prx` and `game_patch.prx`. It loads code **into
the game process**, where patching memory needs no permission at all because the memory is the
process's own. Its cheat library is at `/data/GoldHEN/cheats/{json,shn,mc4}` - the same three
formats ours ships 7,022 of.

That is the mechanism a PS4 engine of ours would have to use, and it needs a `.prx` built against
GoldHEN's plugin ABI. That ABI is not on this machine and is not something to infer, so this
version does not pretend to have it.

### The PS4 says which game is running

`/api/health` has answered `running_title: ""` since the PS4 payload existed, with a comment saying
honestly that no PS4 signature for the question could be read anywhere and that inferring one is
how this project once crashed a console. The caution was right; the premise was not. The payload
SDK ships a working sample that enumerates processes with `sysctl{CTL_KERN, KERN_PROC,
KERN_PROC_PROC}` and asks each pid via `sceKernelGetAppInfo`, and OpenOrbis declares the PS4's own
foreground call - `sceSystemServiceGetAppIdOfBigApp`, **not** the PS5's `...OfRunningBigApp`.

The two toolchains disagreed about the app-info layout: title id at offset 12 with 14 bytes, or at
offset 16 with 10. They cannot both be right, so `/api/engine/proclist` reported **both candidates
as raw text** and let the console settle it. Across all 63 running processes offset 12 was empty
every time and offset 16 held real ids - `NPXS21002`, `NPXS20001`, `NPXS20975`. OpenOrbis is right
for 13.52. A wrong offset yields a plausible wrong pid, and a wrong pid is the one thing a memory
write must never be handed, which is why the probe printed both instead of choosing.

`sceKernelGetAppInfo` resolved from nowhere until `libkernel.sprx` was added to the dlopen list -
`RTLD_DEFAULT` does not reach it, exactly as it reached no BGFT symbol when this file was new. Same
lesson, second library. The answer is memoised for two seconds; health still returns in 16-21 ms.

### Mods & Patches: the PS4 tab is the same section, not a notice beside it

Four faults, all the same mistake - the PS4 tab was written as a fallback rather than as the
section it lives in:

* The PS5 tab lays its cheats out as two columns of tiles; the PS4 tab drew full-width rows, so
  switching tab changed the shape of the panel instead of its contents. Both now render the same
  `.modgrid` of the same `.modtile`s - the PS4's inert, because there is nothing to press.
* The tabs sat in their own strip under the heading, pushing the section down and reading as a
  second console picker. They are now on the heading's own line, after the count, hard right, and
  bigger than the label they sit beside because they are controls.
* **A PS5 game offered a PS4 tab.** A PPSA title cannot be installed on a PS4 at all. The tabs now
  come from `eligibleConsoles()` - the same answer the install picker has used since the
  two-console work - so a PS5 game gets one tab, and one tab means no strip is drawn.
* **The panel collapsed to one column when the PS4 tab had no cheats.** A 900 ms check drops the
  two-column layout when the right side comes back empty, which is right for a title with no cheat
  file - but it re-laid the whole panel out underneath the control the owner had just pressed. A
  section that offers a way back has to still be there to go back to.

The selected tab was also styled with `var(--acc)`, which this stylesheet does not define; it is
`--accent`. An undefined custom property makes the whole declaration invalid, so the selected tab
was never highlighted at all.

`tools/test_mods_tabs.py` pulls those functions out of the page and **runs** them against a fake
fleet - six cases, including PPSA getting exactly one tab. A grep cannot tell you that. It gates
both ELF builds and `ready_check`.

### Icons the PS4 cannot draw

Several icons render as the empty box a font uses for "I do not have this character", the laptop on
the storage tiles most visibly because it is on the front page. Every one was an astral-plane
pictograph and the PS4's browser ships no colour emoji - no font-family list can rescue a glyph the
device does not have. They are drawn now: small inline SVGs, sized in `em` and coloured with
`currentColor`, so no font is involved. Console tiles gained an icon they never had, so a row that
mixes computers and consoles says which is which before you read the names.

One exception, on purpose: the console picker is a `<select>`, and an `<option>` renders text and
nothing else. Those two lose the gamepad and keep the name. The online dot stays - `●`/`○` are BMP
geometric shapes, not emoji.

### The panel says which consoles hold the game

The status badge answers "is it installed?" for the one console the panel is aimed at, which is
what the Install button needs. With two consoles the more common question is the other one, and a
game on both looked identical to a game on one - the only way to tell was to switch the picker and
read the badge again. A new pill lists every console that has it, marking the ones with an update
waiting, built from the same `stateOfOn()` the badge uses so the two cannot disagree. Nothing
renders on a single-console setup.

---

## [3.64.0] - 2026-09-23 - "Asking before answering" `[VERIFIED]`

The rest of that audit: the medium and low findings, re-triaged against the code as it
stands rather than as it stood when they were raised. Everything below was measured, and where a
finding turned out not to matter that is recorded too, with the number.

### Both payloads answer 304, which their own ETag had been promising for months

`send_file()` has sent `ETag: "<size>-<mtime>"` on every static file since caching was added, and
the comment above it says an unchanged shell then "costs only a 304". It could not: nothing read
`If-None-Match` back. A browser politely asking "still the same?" was answered with the whole file,
every time - 811 KB of app shell per reload, down a console's single accept loop, queued behind the
install engine and the cheat engine. The validator was written, sent, and echoed back by every
browser for nothing.

Matching is a substring search over the header line, not an equality test: the value may be a list,
and a cache may hand back a weakened tag (`W/"..."`) for one it revalidates. An equality test misses
both and silently never hits, which looks exactly like the bug being fixed. Threaded down as a
parameter rather than a global, because the PS5 runs one accept loop but the PS4 runs a thread per
connection.

`tools/test_etag.py` extracts the function from both payloads, refuses if the two copies have
drifted, compiles it and runs eleven cases; two of them caught real bugs while it was being written.
It is a gate in both ELF builds.

**The companion had the other half of the same problem.** `_static()` read the whole file off disk
*before* working out that it only had to send a 304 - nothing in that decision ever needed the
bytes. Measured against the running server: an exact echo, a weakened tag and a tag in a list all
now cost 0 bytes instead of a 295 KB gzipped re-send.

### Four memos that expired one second before the poll that reads them

Each was written as 5 s "because health is polled every 6 s". For a single viewer the hit rate was
exactly zero: every memo did nothing but hold data long enough to be stale, while every poll still
paid four probes into a console's single-threaded accept loop.

Measured with the PS4's own connection counter, ten polls 6 s apart against both real consoles,
with the owner's companion polling them throughout in every run:

| memo TTL | connections the PS4 served |
|---------:|---------------------------:|
|      5 s | 36 |
|      5 s | 36 (repeat) |
|     11 s | 29 |

`HEALTH_POLL_S` and `HEALTH_MEMO_S` now sit on adjacent lines, because the relationship between
them - not either value - was the bug.

### The stale `:8791` the owner saw in their browser

Registered PCs were keyed on `(ip, port)` and nothing ever aged one out, so a companion started on
a spare port took a *second* slot instead of updating its own, and the abandoned entry outlived it
for as long as the payload stayed loaded. `best_pc_base` walked down to it whenever the real PC was
briefly away. One machine is one entry now, keyed on the address, and an entry silent for ten
minutes is skipped - in `best_pc_base` and in all four routes that *list* PCs, because each of
those prints `"online":true` or `"ok":true`, which is a claim rather than a reading.

### The PS5 can answer its own engine panel

`/api/engine/state` was companion-only. A PS5 serving its own page fell to the unknown-`/api/` stub
`{}`, and the Settings panel printed its honest "Can't tell from here" with three dark LEDs - on the
very console that was serving the sentence. The busy latch is copied field for field from
`/api/engine/spawn-status` so the two routes cannot drift into disagreeing about it. No `platform`
key, deliberately: a console that does not name a platform is a PS5.

### The closed queue drawer left the focus order

A drawer that has been opened once keeps its rows in the DOM, and an element that is only
transparent is still focusable - so with the drawer shut, Tab and the console's D-pad walked off the
header into the cancel and retry buttons of every queued row. Focus vanished from the screen, and a
press there cancelled a live install with no visible row to explain it.

Expressing it as `transition: visibility 0s linear .16s` did not hold - measured, the drawer closed
and `getComputedStyle` still said `visible` a second later. `visibility:hidden` *and* `inert` now,
set from JS after the fade: visibility is what takes a subtree out of sequential focus navigation
and is all the two older console WebKits understand; `inert` additionally blocks a programmatic
`.focus()`. Measured open/closed/reopened: focusable true, false, true.

*The first measurement of this said "still focusable when closed" and was wrong - the probe reset
focus with `document.body.focus()`, body is not focusable, so focus never left the button and the
check compared it against itself. The instrument, not the fix.*

### All fifteen languages carry the whole dictionary

154 keys x 8 languages - hi it ko nl pl ru tr zh - every one now at 663 of 663.

These are the keys nothing is wired to yet, which is exactly why they mattered: coverage is measured
against the keys the app *uses*, so all eight read 100% while being 154 short, and those are the keys
the next UI change reaches for. Wiring one would have turned a green gate red for eight languages at
once. The gate now also prints the backlog as information it never fails on, so this cannot build up
again unseen.

Checked for the failure that has bitten this project before: a translator handed a prompt rendered as
HTML can return `Settings &gt; Storage`, and these land in a JavaScript string literal where an entity
is shown to the reader literally. Zero found.

### The icon reads `SHOP_PORT`

`#define SHOP_PORT` was used correctly everywhere the home-screen app *connects*, and ignored in the
two places it *tells the owner where to go*: both notifications had `127.0.0.1:8710` written out by
hand. Moving the port would have left the icon directing the owner to the old address - on the one
screen they are looking at precisely because something is not where they expected it.

### One caller takes the PS4's install slot, not twelve

The busy check was check-then-act, and the gap was a whole BGFT registration wide. Every route that
starts a transfer read `active` under the lock, **released the lock**, registered a task with the
console, and only then took the lock again to fill the slot. Requests arriving inside that gap all
read "not busy", all registered a task, and the last one overwrote the slot - so the earlier tasks
went on downloading with nothing following them, while progress, the finished check and the cancel
button all described the last package. The guard that was added for exactly this narrowed the
window instead of closing it.

**Measured on the PS4, in the console's own install log.** Twelve concurrent presses of Start queue,
the same test minutes apart, the two builds differing only in this change:

| | `install: register failed ... RACEPROBE`, same second |
| --- | ---: |
| without the claim | **12** |
| with the claim | **1** |

**Why the PS4 and not the PS5.** The PS5 payload has the same routes and does not have this bug: its
accept loop is single-threaded, so only one request is ever inside a handler, and its own comment
says so. The PS4 spawns a thread per connection. It inherited the shape and changed the concurrency
model underneath it. An independent read of all five PS5 install decision points found every one
already atomic, and `spawn_lane_claim()` there already carries a comment recording that this same
bug was found and fixed on that side. The PS5 is not touched.

Six lanes, not the two first reported: `install_local_pkg`, the direct lane,
`/api/engine/install-spawn`, `/api/queue/start`, a row's `/retry` and `POST /api/install`. The
`mode:"queued"` branch never had it - it tests and fills under one lock hold, which is the pattern
this brings to the rest.

**A token, not a flag**, and that distinction is the whole design. The first version committed
against "is a claim outstanding", which is not the question: a cancel arriving mid-registration
clears the claim - correctly, so the committer knows to hand its task back - but it also frees the
slot, so a *second* install can claim before the first returns. The first would then find the
boolean set again, believe the claim was still its own, and write its task id over the second one's
row. Two live tasks, one slot: the bug, reintroduced by the fix for it. An adversarial read caught
that after the flag version had been written.

**Nothing is ever handed a task id BGFT never issued.** What the service does with one is written
down nowhere - not in `bgft.h`, not in the OpenOrbis headers it was copied from, not in the SDK this
builds against (which has no BGFT header at all, which is why every symbol is `dlsym`'d), not in the
link stub (which is codeless), and there is no GoldHEN source or firmware dump on this machine. The
one measurement the repo holds is about an id that *was* valid and has since been unregistered,
which does not transfer. So the answer is not to ask: the file already encodes that as a convention -
`bgft_release()` returns early on `BGFT_INVALID_TASK_ID` - and everything that could reach the
service during a claim now follows it. `job_refresh()` returns before it even reads the task, the
stranded-task sweeper does not run, and the two cancel routes guard the stop call they were making
**unguarded**. That last one is a pre-existing bug: a direct install has carried
`BGFT_INVALID_TASK_ID` since that lane was written, so cancelling a local install has been calling
`stop(-1)` all along.

And "no task" is `-1` everywhere now, never `0`. `memset` leaves `0`, and `0` is a value BGFT really
issues - the sweepers accept it, observed ids are `0x75` and `0x77`, and the OpenOrbis header shows
PUP and Store tasks share that id space. A zeroed slot reaching a stop call would be asking the
console to stop something that is not ours.

**Found while racing it:** Start queue and Clear are POSTs, their handlers are GET branches, and the
PS4 had no delegation - so both fell through to the `200 {}` at the bottom of `handle_post`. Two dead
buttons on any page a PS4 serves, answering exactly like success. The race test passed by GET and did
nothing at all by POST, which is how it surfaced.

`tools/test_job_claim.py` keeps the race. It installs nothing - the url it queues has no `.pkg`,
which BGFT refuses at registration - and it checks the three things that are easy to get wrong once a
claim exists: the losers are refused rather than queued behind, the row is held again afterwards, and
the slot is not wedged. Verified at 8, 12 and 16 concurrent: one attempt every time.

*Not verified on hardware: a successful install through the new commit path. The abort path runs on
every boot - the dashboard-app reinstall has been failing at registration with `0x80991404` since
before this change - and behaves.*

### The PS4 stops asserting things it never checked

`"ftp_online":true, "ftp_port":2121` was a claim on the strength of nothing, in three routes at
once, so the Files row in Settings sat green and printed an address to type on a PS4 where no FTP
had ever been loaded. FTP is not ours - it is GoldHEN's, up only if the owner loaded it. All three
probe now and all three move together, because fixing one and leaving the others asserting is the
exact miss that cost this project a follow-up release once already.

`POST /api/config` fell through to the generic `200 {}`. The GET half had been fixed, but the page
SAVES with a POST, so Settings painted a green tick over a save that could not have happened and
the next poll refilled every field from a config that had never changed.

**A guard of mine that had become a lock-out.** Both busy checks read `g_job.state`, which does not
advance on its own in that process - it moves when a route reads it. An install that finished while
nobody had the page open left the slot saying "downloading", so the console answered *"An install is
already running on this PS4"* while doing nothing at all, and only reloading the ELF cleared it.
That is the shape of a bug this project has had before. Every other busy check in the file refreshes
first; these two were the pair that did not.

Three routes the PS5 has and the PS4 did not - `/api/helpers`, `/api/fs/delete`, and the
"not supported here" answers for `POST /api/game/delete` and `/api/move` - each of which made
something upstream say the wrong thing. `/api/fs/delete` is the only one here that destroys
anything, so it is the only one with a boundary: strictly inside the shop's own folder, no `..`, and
the folder itself refused however it is spelt.

`running_title` stays empty, and that is checked rather than forgotten. This payload's only view of
the console is `app.db`, which lists what is INSTALLED and knows nothing about a running process.
The PS5 answers it with two calls whose signatures are read off the PS5 side of this repo, not out
of any PS4 header we hold - and inferring a signature is how this project once crashed a console,
inside a shared system daemon where that takes the whole process down.

### The companion stops meaning "the PS5" by "the console"

`reconcile_consoles` ranked `ps5=0`, `ps4=1` and *everything else*`=2`, against a file that says
everywhere else that a console which does not state a platform is a PS5. An entry added to
`config.json` by hand - the exact shape `SETUP-REMOTE.md` tells people to write - sorted behind the
PS4 and made `consoles[0]` the PS4, and dozens of routes still mean "the console" by `consoles[0]`.
Two independent passes found and fixed that same line, which is the clearest possible sign it should
have been pinned by a test the first time. It is now, and the test was checked against the old sort
key first: it fails on it.

A PS5 package with no title id could be sent to a PS4 - the guard tested the name for "PPSA", and a
container whose `param.sfo` would not read arrives with no title id at all. It asks the library,
which already worked out what the file was when it scanned it.

Verified against both real consoles with `dry_run`, so nothing was installed:

| request | result |
| --- | --- |
| PS5 game → PS4 only | 400, *"This is a PS5 game, and the console it was sent to is a PS4"* |
| PS5 game → PS5 | 200, `consoles ["ps5"]` |
| PS5 game → all consoles | 200, `consoles ["ps5"]` — narrowed, not failed |
| PS4 game → all consoles | 200, `consoles ["ps5","ps4"]` |

which is the rule as asked for: **PS4 games install on either console, PS5 games only on the PS5.**

### Costs that were being paid for nothing

Peer discovery fanned out **254 simultaneous threads** from a request path. `/api/devices` probed
every console serially with an 8 s budget each. `app.db` was pulled *whole* on every cold library
refresh, over Wi-Fi, off the console's single accept loop - and between installs it is byte for byte
the same file.

The rule that survives all of it: `installed_titles(force=True)` still pulls unconditionally and
caches nothing. That call is the install-confirm loop asking whether a title has really landed, and
a cached answer there is exactly the lie that rule exists to prevent. All seven confirm sites pass
it; `bgft.db` and `addcont.db`, read *while* an install runs, never touch the cache.

| route | after |
| --- | ---: |
| `/api/health` | 16 ms |
| `/api/devices` | 16 ms |
| `/api/storage` | 29 ms |
| `/api/network` | 83 ms |
| `/api/library` | 16 ms |
| `/api/sources` | 2063 ms cold, 16-48 ms after |

### The page stops describing whichever console answered first

Twenty-seven findings of one kind: the page had a single idea of "the console" and used it
everywhere, so with two machines connected it routinely described the wrong one. The mods lane, the
move dialog, the queue rows, the delete and clean-reinstall confirmations, the destination tooltip,
the header pill and the Settings connection row all name the console they mean now. The header
picker was inert - it re-targeted only the next install POST - and repaints the drives strip, the
Installing card, the devices list and the open panel.

Verified in a browser against both consoles, on a PS4 game: the panel offers **Install on PS5** and
**Install on PS4**; switching to the PS4 changes the destination to *"Installs to: console default"*
and back to the PS5 changes it to *"Installs to: Internal SSD"*. No JavaScript errors at any point.

*Not verified: anything visual. The browser pane reported a 0x0 viewport throughout, hidden or
fronted, so every layout measurement it offers is a fabrication. The DOM and behaviour checks do not
depend on it.*

### Two gates that caught a blank app before it shipped

`check_web.py` stopped the build on `consoleIsPs4()` being called and defined nowhere - its
definition had ridden along with an anchor that a previous commit had already rewritten, so the edit
was skipped and the callers went in alone. It parses perfectly and throws at runtime, which on this
page means a blank app on every device.

And the translation inserter refused two Chinese strings for unescaped double quotes: the translator
had wrapped `Settings > Your games` in straight quotes for emphasis, inside a double-quoted
JavaScript literal, which they would have terminated. Same blank app, in one language.

### Language

All fifteen languages now carry the whole dictionary - 154 keys x 8 languages of backlog cleared,
plus everything new in this release. Raw English dialog calls are down from 28 to 19.

The style gate went from checking 405 strings to **10,647**: it only ever saw text that reached a
`toast()`, `confirm()` or `prompt()`, so a sentence that was defined and translated but not yet
WIRED sat outside it completely - which is how a message naming software this project does not ship
lived in seven languages under a green check.

### Measured, and deliberately left alone

* **`library_signature` walking the whole library tree every 10 s.** Measured on the real library:
  63 directories, 452 files, 1.25 TB, in 13-25 ms. That is 0.2% of one core. The obvious fix -
  skipping directories whose mtime has not changed - would break the settle check, because writing
  into an *existing* file does not bump the parent directory's mtime on NTFS, so a half-copied
  package could be scanned. Left as it is.

## [3.63.0] - 2026-09-23 - "Both consoles, told apart" `[VERIFIED]`

Measured with a PS5 (12.70) and a PS4 (13.52) both awake on the same network, and the desktop app
talking to both at once. The PS5's payload source changed by exactly one token - the version
stamp - as every release does; the rebuilt ELF is staged but was not deployed to the owner's
console, at their request.

### Found by a full read of the whole app, then adversarially verified

151 findings raised, 136 survived verification, 15 refuted by reading the code they described.
Everything below is from that pass and was fixed in this release.

**Two dead buttons, and my own regression.** Both "reload the install engine" controls answered
HTTP 500 with a NameError on every press. A mechanical rewrite earlier the same day added the
request body to every `_bridge_for` call from `_do_POST` downwards *by position in the file* and
swept up `_dpi_reload()`, which is a method - `body` was not a name there. `tools/lint_python.py`
now fails a build on an undefined name or a duplicate dict key, and is a gate in the exe spec.

**Fifty checks that had never run.** It immediately found fifty lines of `ready_check.py` sitting
after a `return`, unreachable since the day they were pasted there - fourteen undefined reads -
including the guard for the rule that `lane` may only be read inside `isBackupTitle()`, which is
the exact regression that block exists to catch.

**Per-console install state.** A title's install state was a fleet fact. On this hardware: 117
installed titles, 106 PS5-only, 7 PS4-only, 4 on both - so for 113 of them the game panel described
the wrong machine as soon as the other was chosen. The library now carries `console_state` per
title beside the existing scalars, which keep meaning exactly what they meant.

**`/api/installed` was deleting the other console's memory.** It asked one console and pruned
`installed.json` - the only record of what is installed when every console is off - down to that
console's titles. Now it prunes against the union, and only when every console answered.

**The PS4 had no request-origin guard.** The PS5 build has had one for a long time. Any page the
console's own browser opened could start an install, take the shop down, write a file as root, and
read the reply (the payload sends `Access-Control-Allow-Origin: *`). Ported verbatim, with the
route list re-derived for the PS4's own dispatch. Every branch measured on the console.

**The PS4 had no queue.** Its server answers unknown `/api/` paths with `200 {}`, so the drawer did
not stay blank during a standalone install - it said "Nothing is installing" while gigabytes were
being written. `+ Queue` also installed immediately, because `mode:"queued"` was ignored.

**Installing from the PS4's own page was broken for most packages.** It called BGFT with no content
id and no size, deriving an id from the file name - which yields a *title* id for any file called
`<Game>-CUSA#####.pkg`, refused with `0x80990008`.

### The console the app is talking about is now chosen, not assumed

`/api/health` described `consoles[0]` and called it the PS5. That is the PS5 only because
`reconcile_consoles` happens to sort it first — so on a PS4-only setup `ps5_ip` carried the PS4's
address, `ps5_online` its readiness, and the header of the shop, running on the PS4, named it as a
PS5 that was ready to install PS5 games.

The PS5 is now selected by `platform != "ps4"` — deliberately not `== "ps5"`, because a config
written before PS4 support existed names no platform at all and a strict match would report an empty
address for a PS5 that is sitting there working.

Two additions beside it, both new keys rather than changed ones:

* `console_list` — every configured console with its platform, address and reachability. `consoles`
  stays the integer it has always been; peers read it.
* `viewer_platform` — which console is *reading* the page, answered from the requesting address. The
  UI is one file served by the PC and by both consoles while the data always comes from the PC, so
  the page could not tell where it was. Extracted as `viewer_platform_for()` and covered by
  `tools/test_viewer_platform.py` (9 checks).

### The engine panel stops inventing a PS5 on a PS4

It printed **"Payload Manager · Port 8084 · answering"** beside a green light on a console that has
no Payload Manager, no port 8084 and no spawned installer. An invented green light is worse than no
light: it is a specific, checkable claim, and it sends someone hunting for a fault somewhere else.

`/api/engine/state` now reports `platform`, `mode: "bgft"` on a PS4, and no port; the row retitles
itself to *Install service*, and the sentences under it name the PS4's own recovery steps instead of
Payload Manager. Ten new strings, in all fifteen languages.

### "Game backups" no longer paints a red alarm on a working PS4

Mounted game backups are a PS5 arrangement. The PS4 payload reports `shadowmount:false` because that
is the truthful answer to "is it running", and the row read it as a fault. There is now a fourth
state — grey, with a sentence saying why — beside on, off and cannot-tell.

### The dashboard app could be killed part-way through starting the shop

`ps4-app/tile-pkg/pms/main.c` had no SIGPIPE guard anywhere, and every byte of the 1.7 MB payload
goes out through `write()` on a socket owned by the jailbreak's payload loader. SIGPIPE's default
action is to terminate: the moment that loader hangs up, the program that was supposed to wait for
the shop and open the browser is gone — no message, no log line. It is a race, which is why the same
build launched perfectly one day and died the next.

`SIG_IGN` process-wide before any socket exists, `SO_NOSIGPIPE` on each socket, the post-write wait
cut from 30 s to 5 s (this program draws nothing once the splash is hidden, so every second of it is
a black screen), one notification so the wait is visibly a wait, and the hand-over logged as *sent*
rather than *taken* — all this end can honestly know.

### A launchable icon can still be the wrong build, and nothing was looking

Measured on this console: the icon was installed, had its launch ticket, opened — and was the
previous day's build. The ELF tries to replace it and cannot, and the reason is now proven twice in
one log, thirteen seconds apart, with the same package, size and content id:

```
register failed rc=0x80991404 ... uri=http://10.0.0.87:8710/pkgfile/0.pkg   console serving itself: refused
install: started task=140    ... uri=http://10.0.0.76:8710/library/...pkg   PC serving it: accepted
```

`0x80991404` is the download service refusing to authenticate with Sony's content network, and
PlayStation Network being blocked is the configuration this shop asks for. The console's only other
lane cannot create a launch ticket, so it correctly declines to replace a working icon with a worse
one — and then had no way to say the copy was old.

`/api/tile/status` now reports `stale` (and `carries_package`, so a lite payload never claims a
comparison it could not make), and the companion's watchdog repairs on *stale* as well as on *will
not open*. Installed from the PC afterwards: `AppInstallApp = 0`, `error=0x0`, four seconds.

### Also

* `tools/snapshot_api.py` records `log_bytes` as volatile. Not `local`: `/api/network` uses that
  name for a boolean decision while `/api/cheats/library` uses it for a count, and one flat name-set
  cannot tell them apart.
* `tools/test_psn_blocker.py` says which blocker answered it instead of failing over a port 53 the
  running companion already owns. The refusals and forwards are still checked.
* The settings PS5 address box keeps its health fallback, now that health can no longer offer the
  PS4's address for it.

---

## [3.62.0] - 2026-09-21 - "The PS4 joins the fleet" `[VERIFIED]`

### What this release is

PKG MUTANT SHOP now runs on a **PS4 on firmware 13.52**, alongside the PS5, from the same app. One
library, one page, one companion, one queue — a second console in the picker. Everything the PS5 side
does is untouched: `ps5-app/onconsole/server.c` has exactly one changed token in this release, the
version stamp, and the install lane was not modified at all.

Everything below was measured on the hardware (PS4 at 13.52, GoldHEN 2.4b18 as the jailbreak layer).
Nothing was carried over from the PS5 side on the assumption that it would hold.

### One ELF for both consoles is not possible, and that was tested first

The PS5 ELF was posted to the PS4's payload loader before a line of new code was written. It was
accepted — HTTP 200, *"payload launched successfully", "format: ELF"* — and **nothing ran**: port 8710
never opened and klog stayed silent. Different SDK, different ABI, different system libraries.

So there are two payloads and one of everything else. `PKG-MUTANT-SHOP.elf` is the PS5's;
`PKG-MUTANT-SHOP-PS4.elf` is the PS4's; both embed the same `web/` directory, both answer the same
API, and both are held to the same build gates. From the user's side there is one app.

### The PS4 gets a real dashboard app — installed, self-updating, byte-verified

> **Status.** **The app is installed on the console.** `/user/app/PKGM00001/app.pkg` holds exactly
> 6,619,136 bytes with `app.pbm`, `app.json` and `app.xml` beside it, `/user/appmeta/PKGM00001/`
> holds our 291,825-byte icon, and the console's own library lists it as *PKG MUTANT SHOP*, version
> 1.00, installed. The package builds clean — `pkg_validate`: 28 checks `[OK]`, 0 failures — and the
> ELF carries it byte-identically at `0x1c6c0`.
>
> What is **not** yet observed: the eboot has never been run, so nobody has pressed the icon and
> watched it open the shop. And the ELF's own install of it has not yet succeeded end to end — it
> tried, was refused, and the refusal is what uncovered the two defects below.

A PS4 had nothing to press. The shop existed only while a payload happened to be injected, and the
jailbreak's binary loader does not survive rest mode - so after one suspend there was no app on the
console at all, whatever had been "installed". `ps4-app/tile-pkg/` builds the thing that was missing:

**`IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg`** - a real, fake-signed PS4 application. Title id
`PKGM00001` (the PS5 tile's identity: one app whichever console it is on, and it cannot collide with
a game, which is `CUSA`/`NPXS`). Category `gd`, content type `0x1A`, flags `0x0A000000` - the shape
of a retail base game, which is the point: `pkg_meta.parse_pkg` reads it exactly as it reads a
retail title, our own integrity check calls it complete, and it installs down the same lane every
other game does. Nothing special-cases it.

**Pressing the icon:** if the shop is already answering on `127.0.0.1:8710` it opens it; otherwise
it hands the payload it carries to the binary loader on `:9090`, waits for the shop, and opens the
console's own browser at it. If neither works it says so on screen, with what to do about it. It
never opens a connection it does not then fill with the payload - an empty connection to an ELF
loader can stop it listening, which is how this console lost its loader twice during this work, from
nothing but a port scan.

**It loads a payload rather than being the server** because an application is suspended the moment
the browser comes to the foreground, and a suspended process stops answering its socket: a shop
served by the app itself would die at the exact moment the page tried to load it. The payload lives
in a long-running system process. The app is the button; the payload is the shop. Nothing in it is
privileged - loopback sockets, its own `/app0`, two public system-service calls.

**The ELF installs it, exactly as the PS5 ELF installs its tile** — it carries the package and puts
it on the console itself, with nothing else involved. That settled the one structural question this
had: the package must therefore carry **nothing**, because an ELF that carried a package carrying
the ELF would embed a copy of itself on every rebuild. One direction only, and both build scripts
say so — the payload build refuses to start without the package.

It is **intelligent about it**, which is the part worth having. Once its socket is listening it
compares the version it carries with the installed app's `APP_VER` from the console's own
`tbl_appinfo`: same version or newer with its `app.pkg` on disk and it does nothing at all, not even
a write; older and it installs over it, which is how the PS4 updates a title; absent and it installs
it. "Installed" is `app.pkg` **with bytes**, never an `app.db` row on its own. Versions compare
numerically, so `1.00` and `01.00` are the same number.

The staged copy goes to `/data/pkg-mutant-shop/` — **our own folder**. Nothing this app does writes
into the jailbreak's folders or files; the jailbreak's only job is to start the ELF. The PC keeps a
copy of the package and a Settings button for putting the app back by hand, and `GET /api/ps4/tile`
reports what the console has, but the PC no longer installs it automatically: two installers
starting together would have the console refuse the second as busy and log a failure for something
that was working.

**Built with the OpenOrbis PS4 Toolchain v0.5.4** - `clang` -> `create-fself` -> `param.sfo` ->
`create-gp4` -> `PkgTool.Core pkg_build`, all fetched by `build-wsl.sh` on first run, with libssl 1.1
unpacked beside the toolchain because PkgTool's runtime needs it and this machine has libssl 3 and no
root. The toolchain's own `hello_world` was built and packaged first, so the chain was proven on
something known-good before ours was trusted. Verified after: the eboot is `SCE Executable (ASLR)
0xFE10` with a FreeBSD ABI, and extracting the finished package shows the payload inside at its exact
1,690,912 bytes.

**If it goes wrong:** `sceAppInstUtilAppUnInstall("PKGM00001")` removes it - the canonical signature
from the toolchain's own header - or delete it from the home screen like any application. The failure
that cost a console on the PS5 side was a registration with no data behind it; this is the opposite,
a complete package handed to the console's own installer, which is the only thing that writes those
records correctly.

### Two defects the console found for us, and one it nearly killed the shop over

**`0x80990033` is "Not supported extension", and the console says so in words.** The ELF's own
install of the dashboard app was refused every time, while the byte-identical file offered from the
PC installed first time — which made the console's own address look like the culprit. It was not:
klog spelled it out, `[BGFT] ERROR: [2239] Not supported extension.` The transfer service reads the
**extension out of the URL** and will not touch a package whose url does not look like one. Ours was
`/pkgfile/0`. It is `/pkgfile/0.pkg` now; the token parser stops at the dot, so it is still token 0.
Two wrong theories died on the way to that — "it will not fetch from its own LAN address" and "not
from loopback either" — both disproved by the same one-line change.

**Serving a package to the console could kill the shop.** With the extension fixed the task
registered and started, and the payload then took *"A user thread receives a fatal signal"*. That is
SIGPIPE: the transfer service opened the stream, gave up part-way, and closed — and the next write
terminated the process. We are injected into a **shared system process**, so that does not just end
the shop, it takes whatever else lives there with it. `SIGPIPE` is ignored now, `write_all` reports
EPIPE instead, and the stream stops when the reader goes away rather than pushing gigabytes into a
socket nobody is holding. This was reachable by any transfer the console abandoned, not only this
one.

**A title id that is not a game's was never read.** The job's title id came from a search for
`CUSA`/`NPXS`, so the one package this shop installs that is not a game — its own dashboard app,
`PKGM00001` — produced an empty one. The install completed on the console and the job sat at
"downloading" for ever, because the finished-check had nothing to look for, and that in turn blocked
the next install as "busy". It is taken from the content id by structure now, which works for any
title id there is.

### Two more defects in that app, found by review rather than by running it

**Every message it drew would have been invisible.** It set the notification's `useIconImageUri = 1`,
which selects the form that draws an icon beside the text — and that form returns success and renders
**nothing** on this console family. This project has been caught by it twice already on the PS5, and
the PS4 payload deliberately sends the plain form for exactly that reason. The app now sends the
identical struct the payload sends. That is the difference between an icon that explains itself and
one that appears to do nothing, which is precisely the complaint that started this work.

**The browser was launched without the user service.** The reference program for opening a URL on a
PS4 — the browser sample in the payload SDK — initialises the user service first and only launches
the browser if that succeeded. A payload inherits a process that has already done it; a sandboxed
application does not. The app now initialises it, launches, and hands it back.

It also tries **two** payload loaders now, in the two framings they use: one takes an HTTP POST, the
other the bare ELF. The rule that keeps this safe is unchanged, and is why there is still no "is a
loader there?" probe — **a connection is never opened unless the whole payload is then written into
it**. An empty connection can stop a loader listening; this console lost its loader twice that way.

`tools/message_report.py --check` now reads the dashboard app too. It had already drifted: its
failure message named the jailbreak software, which the house style has banned since the rule was
written, and no gate was looking at that file. 453 messages checked, none off style.

### The settings row for it does not lie when the console is off

`installed_app_pkg` answers None both for "the console says it is not there" and for "the console did
not answer", and collapsing those to False made a PS4 that was merely switched off report its app as
missing - which the panel then offered to install, and the install would have failed. The console is
asked whether it is there first; only a console that can answer gets to say no. With the PS4 off the
row shows a dash and no button.

### New — the PS4 payload (`ps4-app/onconsole/server_ps4.c`)

* Serves the shared UI and the JSON API on **:8710**, one thread per connection.
* Reads the console's installed games from the PS4's own `app.db` — a **different schema** from the
  PS5's: per-user `tbl_appbrowse_<userid>` plus a key/value `tbl_appinfo` whose `APP_VER` is the
  installed version. Category `gd` is a real game; `gdi` system stubs are skipped.
* Installs packages from the PC over **BGFT**, with the console's own progress and notifications.
* Installs packages **already on the console** — `/mnt/usb0…7` are scanned and listed in
  `/api/library` with `source: "usbN"`, so a stick is installable with every PC switched off.
* `/api/open` launches the console's own browser at the shop, so one tap puts it on the television.
* Notifications, storage, drives, the file API (`/api/fs/*`), icons and the install log all work the
  same way they do on the PS5, so the companion needed no new transport.
* Cheats, mods and patches answer *"not available on the PS4 yet"* — see the limits section.

### New — the companion speaks to two consoles

* Settings has a **PS4 address** box beside the PS5 one, in all 15 languages. Filling it in adds the
  console; clearing it removes it. `reconcile_consoles()` folds the two boxes into the fleet list in
  one place, so nothing else has to know how they map.
* A console's platform is **learned from the console**, never guessed: the PS4 payload reports
  `platform: "ps4"` in `/api/health` and the PS5 payload has never had that field, so a console that
  names no platform is a PS5. A platform saved in `config.json` wins, so a switched-off console is
  still handled correctly.
* The library now merges **every** console's installed games, not just the first. With two consoles
  configured, the second one's games used to be invisible — shown as not installed, and offered for
  installation again. Each game carries `installed_on` with the consoles that have it; the existing
  single-value fields keep describing the first console that does, so a one-console machine behaves
  exactly as before.
* The install hand-off carries the package's **content id, real size and type**, which the PS4
  requires and the PS5 ignores.
* A **PS5 package is refused for a PS4** with a sentence, rather than letting "All consoles" collect
  one console-side refusal per game. The reverse stays allowed: a PS5 runs PS4 games.

### Fixed — auto-discovery corrupted a two-console config

`main()`'s auto-find exists so a fresh install locates the console with nothing filled in, and it
worked by writing one address. With two consoles configured, that write stamped the discovered
address onto **every** entry — so finding the PS4 rewrote the PS5's address to the PS4's, in
`config.json`, permanently. Auto-find now runs only when there is at most one console; anything the
user has deliberately set up is left alone. It also records the platform it found, and only writes
`ps5_ip` for a console that actually said it was a PS5.

### Fixed — `Fleet.bridge()` answered with the wrong console

An unknown console id fell back to "the first console there is". Harmless with one console; with two
it would quietly hand a PS4's install to the PS5. The fallback now applies only when there is exactly
one console, and an unknown id becomes a refusal the caller can report.

### Four PS4-side bugs found by running it

* **BGFT task table filled up and every install was refused.** `0x80990086` on register, with the
  directories of dead tasks still under `/user/bgft/task`. A task only disappears when something
  unregisters it, so a shop that does not clean up after itself stops being able to install anything
  — including packages it had installed an hour earlier. A finished job now releases its own task,
  and a sweep recovers tasks stranded by a payload reload or a crash. A task counts as ours only when
  its record carries a plain-http URL on the `/library/` route; a Store task is https on a Sony host.
  Both halves must match, which is what kept the seven tasks this console already had — the user's
  own Store and firmware downloads, one of them plain http — untouched.
* **A finished job read as still running.** The job state was only refreshed by `/api/engine/job`, so
  a finished or failed install still looked busy to the very next request and the next install was
  refused. Both the busy check and the cleanup route refresh first now.
* **A finished job was rewritten back to "downloading".** Once its task is handed back, BGFT answers
  every progress call with an error — which read as "no error, no bytes yet" and overwrote a job that
  had already failed. The verdict is reached once and then left alone.
* **Raw SCE codes reached the screen.** The house style has always been that a hex code belongs in
  the install log, not on a television, and `tools/message_report.py --check` enforces it — but the
  report never read the PS4 payload, so the rule was not being applied to it. The report now covers
  both payloads (445 messages checked) and the PS4's sentences were rewritten to obey it.

### Known limit — a title the Store has an update for will not install

Measured twice on this console, and worth stating plainly because it is the one thing that does not
work. Before downloading anything, BGFT asks PlayStation Network whether the title has a newer
version. When it does, BGFT builds a **two-part** task — the package we offered *plus* the update —
and then cannot fetch the second part, because the first did not come from the Store.

> METAL SLUG XX (no Store update): `status=0` → installed in 15 seconds, 532,217,856 of 532,217,856.
> Castle Crashers Remastered (Store has 01.04): `status=1` → the task asked for 238,419,968 against a
> 227,540,992-byte package, the extra 10,878,976 being the update, and ended `0x80990004` with
> nothing written.

No task option changes it: `FORCE_UPDATE`, `INTERNAL`, `REMOTE`, `INVISIBLE` and `entitlementType`
0–3 were each tried and all failed identically. Stopping the console from reaching PlayStation
Network is what clears it.

So the shop **says so, and only when it can prove it**: the job reports the merge attempt only if
BGFT asked for more bytes than the package contains and nothing landed on disk. Anything else gets
the plain sentence. The cause is never guessed.

### Known limit — no cheats or mods on the PS4 yet

GoldHEN does not provide the `kexec` syscall the SDK's C runtime expects, so a payload on this
console has **no kernel read/write**. The cheat engine finds a game's memory by walking its CR3 page
tables, which needs exactly that. `/api/cheat*`, `/api/mods*` and `/api/patch*` answer with one
sentence saying so rather than failing in a way that looks like a bug.

### On the PS4 dashboard app

Earlier in this release there was none, and this entry said so. There is now - see the top of this
entry. What remains true is the reason the shortcut was refused: a wrong app registration is what
left a tile that crashed the console on the PS5 side, so the package goes through the console's own
installer rather than being written into the database by hand.

### Getting the payload to run at all

The [ps4-payload-dev SDK](https://github.com/ps4-payload-dev/sdk) builds the payload, and its C
runtime had to be patched or `main()` never ran — `payload_init()` calls `__kernel_init()` →
`kexec(...)`, which GoldHEN does not provide, and treated the failure as fatal. Three edits in
`sdk-goldhen.patch.py` make the kernel steps advisory; the build script applies them automatically
and upstream is left unmodified. Symbol resolution stays fatal.

The last of the three was the least obvious: `__rtld_init` restored the process jail
*unconditionally*, failed, and returned −1 **after** every library had already loaded fine.

Two more things worth writing down:

* **Never `_exit()`.** GoldHEN injects the payload into a shared host process (`ScePartyDaemon`), so
  `_exit()` tears that process down — including the copy of the payload just loaded to replace this
  one. `/api/quit` closes the listening socket and returns instead.
* **`RTLD_DEFAULT` is not enough.** Every BGFT symbol came back NULL from the default scope and
  resolved fine from the handle of the library we opened. The first build of this file refused every
  install with "BGFT is incomplete on this firmware", which was our own lookup being wrong.

### Tooling

* `ps4-app/onconsole/build-wsl.sh` — fetches and patches the SDK the first time, then runs **the same
  gates the PS5 build runs** (`check_web.py`, `i18n_report.py --check`, `message_report.py --check`),
  because both payloads embed the same UI and say the same sentences.
* `tools/ps4_sync_sqmini.py --check` — a build gate that keeps the PS4's copy of the PS5 payload's
  SQLite reader honest, rather than letting two copies drift.
* `tools/stamp_version.py` now stamps the PS4 payload too. Left out, it drifted immediately: it was
  written at 3.62.0 while everything else said 3.61.0, which is the exact failure that file exists to
  prevent.
* `tools/message_report.py` now reads both payloads.
* `ps4-app/onconsole/README.md` — what was measured on the console, why each decision was made, and
  the SCE codes behind each one.

### The defect that cancelled our own installs — and the proof that replaced it

An update of the dashboard app was reported on screen as a progress bar that stopped near the start
and never finished. It was not the console and it was not the network. **This shop was cancelling its
own install**, about one second after starting it.

`job_refresh()` decided an install had finished from `installed_app_pkg(tid)` — `/user/app/<TID>/app.pkg`
present at roughly the expected size. That is honest proof for a **first** install: there was no file,
and now there is one. It is worthless for an **update**, because the file is already on disk, at very
nearly the same size, belonging to the version being replaced. So the first poll after the task started
saw a file, called the job `installed`, and `bgft_release()` then *stopped and unregistered the running
task*. The PC polling the job immediately after starting it is what pulled the trigger; the earlier
`01.01` update survived only because nothing happened to poll during its four seconds.

Measured on 13.52, in the console's own words:

```
[BGFT] task(00000077) tx started (65536/6619136)
[BGFT] task(00000077) tx started (524288/6619136)
[BGFT] task(00000077) tx stopped (524288/6619136)
Task 00000077 ... ended (state=0,runstate=2,error=0x0)
```

524,288 of 6,619,136 — 7.9%, `error=0x0`, nothing refused anything. The wreckage it left is the part
worth remembering: **`app.db` carried the new version number while `app.pkg` still held the old
bytes**, because the console writes the version early and promotes the file at the end. A shop that
checks only the version then reports itself up to date for ever, on a console that is not.

`tile_thread()` had the same bug and a worse consequence: its wait loop fired on the first tick of an
update and `install_path_cleanup()` **deleted the staged package the console was still downloading**.

Fixed, and the fix is about what counts as evidence:

* **`app_pkg_facts()`** reads size *and* mtime. Every install now records what `app.pkg` looked like
  **before** it started (`job_baseline_locked()`, called in both install lanes), and nothing counts as
  finished until that file **changes**. For a first install this is the same test as before — no file,
  then a file. For an update, only the console replacing the file ends the job. The console writes
  `/user/app/<TID>/app.pkg` last, in `AppInstallApp`, so the change is the honest signal.
* **`tile_bytes_match()`** compares the installed package against the copy inside this very ELF, 64 KB
  at a time, no allocation, stopping at the first difference. A version number cannot detect the state
  the cancellation left behind; bytes can, and this console has now twice been measured storing
  `app.pkg` byte-identical to the package handed to it. An unreadable file counts as a match — not
  being able to read something is not evidence that it differs.
* **`tile-repair.stamp`** bounds that repair to **one attempt per build**, so if some future firmware
  ever re-wraps `app.pkg` as it installs it, the shop reinstalls its own icon once and not on every
  boot.
* **`install_path_cleanup()`** now sweeps every `pms-tile*.pkg` in `/data/pkg-mutant-shop`, not only
  the one the running build happens to name — a copy staged by an older build is the same 6.6 MB of
  dead weight. Still bounded to that one directory and that one name pattern, and still only after the
  install is proven.

The console was repaired the same way it will be from now on: the package was installed from its own
storage over loopback, and `/user/app/PKGM00001/app.pkg` verified byte-for-byte against the build.

### The console's own download table is the scarce resource, not ours

Auditing the BGFT table after the fix: 11 of ~12 slots were in use and **nine of them were the
console's own Store tasks**, eight for a single title (`CUSA23827`), plus a system firmware
`PS4UPDATE.PUP` task. Ours were two, and the sweep released both.

This is worth writing down because it changes where "the table is full" comes from. `bgft_sweep_ours()`
releases a task only when that task's own record contains a plain-http URL on `/library/` or
`/pkgfile/` — our routes. A firmware task points at `dus01.ps4.update.playstation.net`, so it can never
match, and **nothing in this project starts, resumes or cancels a task that is not ours.** Store tasks
stranded in that table can only be cleared from the console's own download list, by its owner.

### CE-32930-7 solved — and the cause was this shop, not the jailbreak

The dashboard icon installed, showed, self-updated, and would not open: `CE-32930-7`. It now opens.
The cause was **our own install engine giving back the BGFT task that a title needs in order to
launch**, and getting there took two wrong answers first — both recorded here, because the wrong
turns are the useful part.

**What the console actually does.** Captured on port 3232
(`research/klog-ce32930-7-app0-mount-2026-09-21.txt`):

```
[BGFT] [606] GameWillStart(PKGM00001, 2) start
[BGFT] ERROR: [3568] task not found. (PKGM00001)
[BGFT] [608] GameWillStart(PKGM00001, 2) end
[PS]Error: process_starter\process_mount.cpp at 3577
sceBgftNotifyGameWillStart() ret = 80990019
[PFS] umount[0x…6fda] finished 0
[PFS] umount[0x…0efc] finished 0
PrepareProcessLaunchPkg() ret = 80990019
lnc_mount_root.cpp(425)  mountApp0Dir:      LNC_ISOK::0x80990019
```

The package's two PFS images **mount successfully** a few lines earlier. Then ShellCore calls
`sceBgftNotifyGameWillStart`, BGFT answers **`task not found`**, that returns `0x80990019`, and
ShellCore unmounts what it had just mounted and refuses to start the process. `PrepareProcessLaunchPkg`,
`mountApp0Dir` and `initializeApp0Dir` all report that same code; none of them is where it came from.

**Two wrong answers, both from reading a filtered log.** The first blamed the eboot's
`SCE_NEEDED_MODULE` list. The second — written into this changelog — said the console could not mount
`/app0` for any fake-signed package and put the blame in the jailbreak layer. Both were wrong for the
same reason: a grep keyed on the `<118>` log prefix, which silently dropped the two lines that matter.
`[PFS] mount … finished` and `sceBgftNotifyGameWillStart() ret = 80990019` carry no such prefix, so
the first surviving line — `PrepareProcessLaunchPkg` — got read as the origin. **Check the instrument
before believing the diagnosis.**

The Riptide GP2 control test was also over-read. It failed identically, which felt conclusive, but
Riptide was installed by *this shop's own lane* too — so "fake-signed" and "installed by us" were
perfectly confounded and it never distinguished the two. It ruled out our package bytes and our
eboot. It did not rule out our engine.

**The proof.** One BGFT task was left registered for the title, deliberately not released, and the
icon pressed again:

```
[BGFT] [606] GameWillStart(PKGM00001, 2) start
[BGFT] [576] task(00000075) PKGM00001          ← found, instead of "task not found"
[Syscore App] createApp PKGM00001
   processParam: elfPath = /app0/eboot.bin
EXEC /app0/eboot.bin [user], vm#1, dmem#1
[PMS] PKG MUTANT SHOP app starting
```

It launched, and our own code ran for the first time.

**So a finished BGFT task is the title's launch ticket, and this shop was throwing it away.**
`bgft_release()` handed the task back the moment a job reached a terminal state, and
`bgft_sweep_ours()` cleared every finished task of ours before each register. Both exist for a
measured reason — a stranded task makes the next register fail — but between them they quietly made
**every title this shop installed unlaunchable, games included.** Riptide GP2 and METAL SLUG XX were
in exactly that state.

Fixed by changing what gets reclaimed and when:

* **`bgft_task_title()`** reads the title id out of a task's own record, recognising a content id by
  its fixed punctuation (`......-<9 chars>_NN-`) in a binary blob full of NULs.
* **The sweep keeps a task whose title actually has an `app.pkg` on disk.** Only tasks whose title
  installed nothing — failed, abandoned, orphaned by a payload reload — are released on sight.
* **Only the title being installed gives up its task** (`bgft_release_title`), because it is about
  to be replaced anyway. No other title is touched, so nothing loses its place on the home screen
  so that something else can install.
* **A successful job no longer releases its task at all** — only a failed one does, because a failed
  job installed nothing and its task is pure waste.

**And the "~12-slot table" this port believed in does not exist.** klog prints the names beside the
codes: `0x80990086` is `SCE_BGFT_ERROR_CONTENT_ALREADY_DOWNLOADING`, `0x80990088` is
`SCE_BGFT_ERROR_SAME_APPLICATION_ALREADY_INSTALLED`. **The conflict is per content id.** Releasing
tasks always looked like "making room" because the one released held that title's id. Measured since:
registering works fine with thirteen directories in the table. Most of those are not ours anyway —
**9 of 11 belonged to the console itself** when audited (eight for one title, plus a system update).

### App 01.04 — the crash on exit, removed from the binary rather than avoided

Launching the icon worked but left an error to dismiss every time (`CE-34878-0`), after the shop had
already opened. The crash dump named the chain precisely:

```
# signal: 12 (SIGSYS)   thread name: eboot.bin
# rip: 00000008000028bc   BrF: 0000000000406c20   BrT: 00000008000028b0
```

`SIGSYS` is a system call the process may not make. `BrF` sits in this eboot's own PLT, `BrT` in
libkernel, and the fault is twelve bytes further in — so: `return` from `main()` → the toolchain's
`exit` → `_Exit` → `_exit@plt` → libkernel's `_exit` → its syscall, refused to a sandboxed
application.

**`_Exit` is now defined in `pms/main.c`.** The toolchain's libc keeps `_Exit`, `exit` and `_exit` in
three separate archive members (`ar t libc.a` → `_Exit.lo`, `exit.lo`, `_exit.lo`), so ours satisfies
`exit`'s reference and `_exit.lo` is never linked at all. Checked on the built binary: `nm -u` lists
no `_exit`, and `objdump -d -j .plt` has no `_exit@plt`. The faulting instruction is not merely
unreachable — it is absent, so no stray `exit()`, `abort()` or `return` can find it again.

`main()` now ends with `sceSystemServiceLoadExec("exit", NULL)`, the documented way for a title to
close itself, and parks if that is refused. Parking is not lovely — the app sits suspended showing
nothing and has to be closed with the PS button — but it is silent, and an error dialog after every
single launch is not. The return code is logged so the next reader knows which happened.

### What the first successful launch exposed in the app

```
[PMS] load /system/common/lib/libSceSystemService.sprx -> handle=-2147352574 res=0x00000000
[PMS] resolved: browser=0 user_init=0 notify=0
[PMS] the shop answered /api/open: HTTP/1.1 200 OK
```

* **Run-time resolution by absolute path cannot work from an application.** `-2147352574` is
  `0x80020002`, `ORBIS_KERNEL_ERROR_ENOENT`, and the crash dump says why: this process had its system
  libraries mapped at `/vm2LJNGVpN/common/lib/` — a per-sandbox random prefix — so
  `/system/common/lib` is not a path it can see. Every pointer was `NULL`, which means
  `sceSystemServiceHideSplashScreen` had **never once run** despite the comment claiming it fixed the
  blank loading screen. Linking is how a sandboxed app reaches these, so the app now links
  `-lSceSystemService` and calls them directly.
* **Notifications never needed any of it.** `sceKernelSendNotificationRequest` is a libkernel export
  declared in `orbis/libkernel.h`, and `-lkernel` was on the link line the whole time. It was being
  looked up through a handle that could never open.
* **The direct browser call is deleted.** The toolchain declares
  `void sceSystemServiceLaunchWebBrowser();` — no url, no return — so the four-argument call site was
  a guess with a confident comment attached to it. `/api/open` to the unsandboxed payload is measured
  working on this console and is now the only route. One route that works beats two where one is
  invented.

Also gone with it: the hand-copied notification struct, `load_module`, `sym_of`,
`resolve_everything`, and the `-lSceUserService` / `-lSceSysmodule` that existed only to serve the
deleted call.

### What the app found once it finally ran

The launch also produced the first real evidence about the eboot, and it is not flattering:

```
[PMS] load /system/common/lib/libSceSystemService.sprx -> handle=-2147352574 res=0x00000000
[PMS] resolved: browser=0 user_init=0 notify=0
[PMS] the shop is answering - opening it
[PMS] the shop answered /api/open: HTTP/1.1 200 OK
[SL] AppFocusChanged [PKGM00001] -> [NPXS20001]
# A user thread receives a fatal signal
# signal: 12 (SIGSYS)   thread name: eboot.bin   rip: 00000008000028bc
```

* **Every module load failed**, so every symbol was null and the app's own browser call was dead.
  The crash dump says why: inside the sandbox the system libraries are mapped at
  `/vm2LJNGVpN/common/lib/`, not `/system/common/lib`. Loading them by that absolute path cannot work
  from an application.
* **The `/api/open` fallback is the only reason anything appeared on the television.** Asking the
  unsandboxed payload to open the browser worked first time — the one design decision in that file
  that has earned its place.
* **Then the app died with `SIGSYS`** on the way out of `main()`, which is the error dialog that has
  to be dismissed after the shop opens. Bad system call, `rip` inside libkernel, reached from our own
  text: the toolchain's exit path is making a call an application sandbox does not allow.

The module paths and the exit are being fixed properly rather than guessed at; this release records
what was measured. The shop itself is unaffected either way — it has never needed the icon.


### Verified on hardware

* The payload runs as root, serves the 762 KB shared UI on :8710, and hot-reloads over a running copy.
* `app.db` read with real names and `APP_VER` versions; `/api/installed` lists only titles with their
  own `app.pkg` on disk.
* **Riptide GP2** (107,806,720 bytes) and **METAL SLUG XX** (532,217,856 bytes) installed end to end
  from this PC's library — exact byte sizes on disk, `app.pbm`/`app.json`/`app.xml` present,
  registered in `app.db`, `sceAppInstaller::AppInstallApp` → `0x00000000`.
* The task sweep released exactly the three tasks the shop had stranded and left the console's own
  nine alone.
* A failed install reports as failed, is cleared by the next `spawn-cleanup`, and the install after it
  starts normally.
* **The dashboard app updates itself and the result is byte-verified.** `01.00` → `01.01` → `01.02` →
  `01.03` on the hardware, each one installed from the console's own storage over loopback, and after
  the last two `/user/app/PKGM00001/app.pkg` was read back over FTP and hashed: `01.02` =
  `b3988c3e…64`, `01.03` = `5968c773…fb`, each identical to the package the build produced. `app.db`
  reports `01.03`.
* **The staged copies are gone afterwards.** `/data/pkg-mutant-shop` holds `install.log` and `web/` and
  nothing else — no package left behind on the drive.
* **The task table was left as it was found.** Ours released, the console's nine untouched, the system
  firmware task neither started nor cancelled.

---

## [3.61.0] - 2026-09-04 - "The audit pass: 262 findings, and the sync that never converged" `[VERIFIED]`

### Where this release came from

A read-only audit of 3.60.0, end to end: the companion, the console payload, the page, the tools,
the build and every document: one subsystem and one lens at a time, then every finding re-read by
a verifier told to assume it was wrong. 339 raw findings became **262 confirmed, 12 refuted and 65
left unverified**. The report is the audit dossier; this entry is what was done about it.

Then a fix pass on four file lanes (companion, console, page, tools+docs), each change reviewed
adversarially against a git baseline and re-checked by hand. **This release also starts the
repository's history**: commit `4fe2409` is byte-identical 3.60.0, so every line below is a
`git diff` away.

Nothing here changes what the app does. The install lane came through the audit clean and was
not touched where it matters - see "What was deliberately left alone" at the end.

### The cheat library re-sent 2,092 files every fifteen minutes

Every `[cheats] console is missing 2092 file(s)` line in pms.log since 25 August was the same
arithmetic. The console's directory listing was capped at **96,000 bytes** - about 1,265 entries -
and the three big cheat folders are larger than that:

| folder | on the PC | console reported | re-sent |
|---|---|---|---|
| json | 1994 | 1265 | 729 |
| mc4 | 2142 | 1262 | 880 |
| shn | 1763 | 1280 | 483 |

729 + 880 + 483 = **2092**. The four small folders (376, 369, 369, 9) reported correctly. The
console said so honestly - `"truncated":true` on every capped page - and the PC never read the
flag, so it computed "missing" from a partial listing, re-sent files that were already there, and
did it again fifteen minutes later. About 30 MB and 45-90 s of console I/O per pass, on the
single-threaded accept loop the UI and installs share.

Both halves are fixed. The console lists into a 1 MiB heap buffer that doubles to 8 MiB before it
will admit truncation. The PC honours `truncated`: a folder it cannot list in full falls back to
the FTP listing when there is one, and otherwise is reported as unknown and **nothing is sent to
it**. The 15-minute thread and `POST /api/cheats/sync` are mutually exclusive now.

The library itself lost its dead weight. `xml/`, `xml_orbis/` and `xml_prospero/` were 747 files
the console writes to disk on every boot and never reads (368 of 369 in each are byte-identical to
`patches/`; the three that were not are copied into `patches/` by the packer). The 706
`<name>.mc4.xml` twins in `mc4/` are unreadable by the engine and produced phantom versions like
`01.00.mc4` in the version list. The pack went from **7,022 files / 32.2 MB to 5,569 / 27.2 MB**,
and the companion syncs the same four folders the console reads.

### Four protective holds that raised instead of holding

`Queue._pause_pending` was decorated `@staticmethod` and declared `self`. Its four callers - no
verdict from the installer, an unknown host, a wedged hand-off, and the console being switched
off with jobs queued - each passed two arguments to a function that wanted three. Every one of
those safety paths raised `TypeError`, the worker's catch-all painted the job red with "This one
stopped unexpectedly", and the rest of the queue marched into the same failure. The behaviour
described in 3.17-3.29 had never once run. The decorator is gone; the four holds now hold, and
Start queue releases them.

### The install lane, tightened without being changed

- **A stopwatch no longer overrides the evidence.** The promoting loop's 600 s cap sat below the
  1,800 s "row has not moved" rule, so that rule was dead code and a large title still copying
  after ten minutes ended as `stale_install` with advice to delete it. The cap is 2,100 s; when it
  is reached with no evidence either way the job ends `install_unconfirmed` and says the console
  may still be installing. The acceptance conditions are byte-for-byte what they were.
- The loop's heavy reads (a full app.db pull plus four folder scans, ~40 requests per 3 s tick)
  run every 15 s; the bgft row stays on the 3 s cadence.
- `install_spawn` pops its 120 s debounce entry on every early return, so the retry-once and a
  Start pressed on a held job are no longer refused by our own debounce with the real cause lost.
- Cancel is read between phases, not only at the top of `_run`; a cancel during the mount pull
  lane asks the console to stop the download.
- **The verdict now carries a token.** The shop writes a per-request token as line 3 of
  `installer-req.txt`; `pms-installer.elf` echoes it into `installer-res.json`, and a verdict
  without this request's token is ignored, so a late result from an installer that was still
  pre-allocating when the lane gave up can no longer be read as the next job's. The installer
  consumes the request file the moment it has read it, so a copy launched by hand from Payload
  Manager installs nothing. Both ends keep every existing field and the file names.
- The spawn latch's check-then-set is one step under a mutex; `pm_get` counts only an HTTP 2xx (or
  the loader's `OK`) as a spawn and waits 10 s, not 30, on the accept thread.
- Every hand-off and every verdict is one dated line in pms.log now, with the task id.

### Console safety

- **The move lane wrote into a watch folder under its final name.** `move_thread` copied straight
  to `<drive>/homebrew/<name>`, which is the partial-mount trap recorded in 3.24 on a different
  lane. It copies to `.part`, keeps the size comparison, and renames. `/api/move` accepts only a
  backup under the homebrew roots and checks the destination drive is really mounted (the same
  `st_dev` test `/api/devices` uses) before copying.
- Rest mode refuses - unless forced - while a spawn hand-off is live, a move is running, or an
  upload is in flight, not only during a download. The PC relay stops answering "safe to rest" on
  a timeout: only a connection the console closed after acknowledging counts, and it refuses while
  our own queue has a running task.
- `restart_shadowmount()` asked `running_title()` for `title_id`; that function returns `titleId`.
  The busy guard could never fire, so ShadowMount could be reloaded under a game running off a
  backup. One key name.
- Delete-backup uses `lstat` and never follows a symlink; a container that is still mounted is
  reported as staying playable until the console restarts, which is what happens.
- Cheat engine: the expect-gate ran only when the on and off byte strings were the same length -
  549 real entries were written blind. It now reads `max(on, off)` bytes and gates every non-forced
  write. The patch undo file was truncated on every Apply; it is created once (per installed
  version) and appended, so a second Apply cannot destroy the originals. A partly applied patch says
  so. `hex2bytes` refuses an odd digit count instead of dropping a nibble. Cheat lines up to 64 KiB
  and entries up to 4,096 bytes parse instead of vanishing; a mod with dropped entries is refused
  rather than reported ON.

### The console API

- **Uploads no longer freeze the console.** `/api/fs/write` runs on its own thread with the socket,
  exactly like `/pkgfile/`, with a 120 s receive timeout instead of the accept loop's 8 s - so a
  brief PC stall no longer aborts a 90 GB push, and the UI keeps answering during it. A body with
  no Content-Length is refused instead of becoming an empty file.
- Any page in any browser on the LAN could fire a state-changing GET with no Origin and no
  Referer. Both servers now also refuse `Sec-Fetch-Mode: no-cors|navigate` and image/script/frame
  destinations on state-changing routes only; absent headers stay allowed, and the app's own
  `fetch()` calls are untouched. `host_is_private` consumes the whole token, so
  `10.0.0.1.attacker.example` is a name, not a private address.
- `/api/fs/delete` and `/api/fs/mkdir` are bounded to the roots the file API already trusts;
  `/api/engine/lprobe` and `/api/engine/install-url` sit behind the diagnostics flag. The notify
  icon probe no longer sleeps on the accept loop. Cheat routes parse a document once per request.
- `appinst_once()` runs under a mutex. `sceAppInstUtilInitialize` is still called exactly once,
  lazily, and `sceAppInstUtilAppInstallPkg` still has exactly its two call sites.

### The companion

- The page is sent gzipped with an ETag and answers 304, and static assets carry a week of cache;
  `index.html` is compressed once per build and served from memory.
- A 300 s idle timeout reaps connections that never send a request line; the socket a package
  streams over is explicitly exempt. Listen backlog 64. HEAD answers what GET would.
- `_origin_ok` gained the same Sec-Fetch rule, applies to the state-changing GET relays too, and
  accepts the page's own hostname - so opening the PC by name no longer gets 403 on every POST.
- `config.example.json` is a template again, not a live config layer; a saved `shadowmount.port`
  of 9021 (elfldr) is read as 10101 in memory. `/api/engine/state` says `spawn`, which is the
  only lane there is.
- `helper_status()` is memoised for 5 s (its comment always said so), `console_usb_packages()`
  for 20 s, missing console icons for 10 minutes; `federation_self()` asks for the LAN address
  once per call, not once per game. A peer that re-identifies drops its old entry.
- `installed.json` is written only when it changes, under the lock. pms.log rotates at write time,
  not only at start. Handler exceptions log one line and answer 500 JSON instead of an empty reply.
- At boot the frozen exe sweeps its own orphaned `_MEI*` extraction folders (170 of them, 1.2 GB,
  were in %TEMP%); only folders carrying our page and older than a day, never the live one.
- A backup file name outside latin-1 no longer aborts `/library/<key>` before the first byte
  (RFC 6266 `filename*`). `/api/hash` refuses files over 8 GiB instead of reading them on the
  request thread.

### The page

- `api()` has a timeout (12 s default, longer where a route is known to be slow) and a non-2xx
  answer reaches the caller as a refusal with the server's words. `refreshHealth` cannot pile up.
- **The console page fails over.** When the PC companion misses three health polls in a row the
  page repoints itself at the console's own API, says so in the pill, and keeps looking.
- Toasts are set as text (never markup), stay 6.5 s for errors, dismiss on click, stack to five.
  Peer names, library paths and format strings are escaped everywhere they reach `innerHTML`.
- Save in Settings and a language change no longer push a second panel entry that froze grid
  virtualisation and swallowed a Back press. A mod tile keeps its name after one toggle. The drive
  picker re-reads free space when its list is stale or was the static fallback. Queue rows say
  "PS5 decides the drive" for a package instead of printing `internal`, and the queue keeps polling
  while the dock is open so installs started from another device appear.
- `Open it on the TV` read `/http://10.0.0.99:8710`: the start-ellipsis on `.svc .v.ell` uses
  `direction:rtl`, which moves a trailing slash to the visual front. The value is written into a
  left-to-right span with an LRM guard. The Installing card explained the engine twice; it now
  keeps the server's sentence.
- The 29 keys used only through `tsub()` had never been translated into the eight newest
  languages, and the gate could not see them. They are translated; the gate counts them.
- Contrast (`--muted2` to 5.0:1), 44 px touch targets under a coarse pointer, reduced motion
  covering every animation, focus moved into a panel on open and back on close, RTL mirroring for
  the search glyph and chevrons, a sticky Settings header. "Running v1.05" became "Installed
  v1.05"; the header pill says "ready to install" instead of engine jargon.

### Tools, build and documents

- `tools/i18n_report.py` counts `tsub()` and ternary keys. `tools/message_report.py` bans the
  words its docstring promised (etaHEN, Elf Arsenal, DPI, :12800) and now sees server-side error
  sentences: 438 messages checked, 0 off style. `tools/check_web.py` has real ES5 rules, not only
  a parse. `tools/stamp_version.py` stamps three targets and refuses if one is missing.
- The exe excludes numpy (Pillow never needed it): **26.8 MB to 15.6 MB**, and 26 MB less unpacked
  per launch. The spec and `build-wsl.sh` run the i18n, message and storage-tile gates as fatal,
  `build-wsl.sh` refuses to link a stale bundle, and the web bundle is an allow-list.
- `tools/ready_check.py` no longer deletes a real-looking title id or writes into a watch folder,
  and refuses to hand the console `127.0.0.1`. `tools/verify_console.py` clears the install latch
  only when it is stale, and stages 5-7 are opt-in. `doctor.py` checks the engine that exists.
- `deploy.py app` is retired (it pushed the whole 92 MB `ps5-app/` tree into ShadowMount's watch
  folder). **`deploy.py elf` is the scripted console update**: FTP probe, `.part` upload to Payload
  Manager's registered path, pre-quit busy checks, quit, reload, and a wait for the new version.
  `register_tile.py --apply` refuses without an explicit flag.
- `SETUP.md` is the one current runbook. README is rewritten to the truth; ARCHITECTURE, TOOLCHAIN,
  ROADMAP, ROADMAP-OVERHAUL, BUILD-PS5-APP and SETUP-REMOTE carry dated superseded banners and
  corrected sentences; the engine manual's sections marked current were re-read against the code.
  `LICENSE` (GPL-3.0) and `THIRD-PARTY-NOTICES.md` exist. `config.example.json` mirrors
  `DEFAULT_CONFIG`.

### Addendum, 2026-09-04 (later the same day) - ShadowMountPlus updated

The ShadowMountPlus build embedded in the console ELF (`payload_bundle.h` ->
`payloads/shadowmountplus.elf`) was replaced with the newly released one: 2,465,896 bytes,
md5 `3aaf3e4f…`, in place of the 24 August build (1,762,880 bytes, md5 `faa6cf21…`). The ELF was
rebuilt at the same version - **3.61.0 stays 3.61.0** - and redeployed; the exe does not carry
ShadowMountPlus and was not touched. Nothing else changed.

Two things worth knowing, read out of `payload_bootstrap()`. The shop writes its embedded copy to
`/data/pkg-mutant-shop/payloads/` and launches it **only when nothing already owns port 10101** -
with ShadowMountPlus up, the whole step is skipped, so after this reload the on-console copy was
still the old 1,762,880-byte file and the running instance is unchanged; the new build lands
there at the next boot or rest-wake where ShadowMountPlus is not yet running. And the launch goes
through Payload Manager, which resolves `/loadpayload` **by basename to its own registered copy**
when it has one (the pldmgr trap): so the copy Payload Manager holds must be updated too - the
file under `ps5_autoloader/` on the Desktop is that one, and the app never reads it.

### What was deliberately left alone

- The install verdict conditions (`_bgft_before_ok`, following a live bgft job from a
  `console_busy` hold, gating the retry-once on SCE codes). Each changes when a job is called done;
  that is an owner decision, not a fix.
- HTTP/1.1 keep-alive on the companion, moving the console's state-changing routes to POST, and
  reflecting Origin instead of `*`: each needs the console page verified on hardware first.
- The ~216 dictionary keys nothing uses (about 110 KB of the page): a cleanup, not a fix.
- Bounding cheat writes to the game's image needs the module map the engine does not read.
- Naming (Add-ons vs DLC, Installed vs MOUNT vs Mounted) and a confirmation before Re-send backup.

### Verified

`py_compile` on every Python file; `check_web` (node-parsed, ES5 rules); i18n 15 languages x 291
live keys; message style 438/0; `test_storage_tiles`; `prospero-clang -Wall -fsyntax-only` on
`server.c` and `installer_probe.c` - the same 7 pre-existing warnings as 3.60.0 and none new
(two dead-declaration warnings went away). Install call-site counts, the `RUNNING` set, and the
`/api/health` key set (plus one additive key, `config_path`) compared against the baseline. The
ELF embeds the rebuilt `pms-installer.elf` (built `-g`) and the 3.61.0 page verbatim; the exe
carries the same page.

---

## [3.60.0] - 2026-09-02 - "Fifteen languages, and the panel says what it is" `[VERIFIED]`

### Fifteen languages, all complete

Eight added - **Chinese, Hindi, Russian, Italian, Korean, Turkish, Polish, Dutch** - chosen by
number of speakers. With English, Spanish, Portuguese, French, German, Japanese and Arabic that is
**15**, and `tools/i18n_report.py --check` reports every one of them at **100%**.

**The wiring more than doubled: 94 keys live, now 213.** 117 edits were located across the game
panel, the mods grid, the cards, the drives row and the queue.

**The coverage tool was measuring the wrong thing and said so confidently.** It compared each
language against every key DEFINED (503) rather than every key USED (213), so eight fully-translated
languages read as "42.3% complete". The dictionary carries keys nothing asks for yet; translating
those is work no one can see. It now measures live coverage, which is the number that means
something.

**The language selector derives from the dictionary.** Adding a language is one step - add its
dictionary - and the selector offers it, because it filters its name list against `I18N`. It showed
7 before these eight landed and 15 after, with no separate list to keep in step.

### The edit reviewer died, so the edits were verified by measurement instead

Part of the review tooling fell over, so nothing was applied on trust. A validator checked, for all 117: that the `find` text exists in the
real file **verbatim and exactly once**; that every `t("key")` in the replacement exists in the
dictionary; and that nothing introduces `title=` or a ninth `scard`/`sec_` match. It rejected 11 -
eight referencing keys that do not exist and three with stale anchors - and 106 applied with zero
skips. Measuring beat reading.

The eight new locales were validated the same way: every placeholder (`{n}`, `{id}`) preserved
exactly, no brand or acronym translated away, nothing three times longer than its English button.
**Zero problems across 8 languages x 213 keys.** MOUNT survived verbatim everywhere, which three
languages had got wrong in the previous pass.

### The settings header on a phone

Three grid columns - back, a centred `nowrap` title, the controls - do not fit 375px, so the
language selector rode over "SETTINGS". Two rows on a phone: back and title, then the controls at
full width. Measured: title `[128-355]`, selector `[39-257]` on its own row, no overlap.

### The controller legend is gone

It was drawn only under `html.ps5` - but a phone opening the console's own page **receives that
class**, because it comes from the `on_console` server fact rather than a UA sniff. So it was never
console-only, which is why it turned up on an iPhone. Markup and all three rules removed.

### The status pill moved beside the game title

`Installed` / `Update available` / `Not installed` sat among a dozen grey detail pills, where the
one fact you want at a glance looked like all the others. It now sits to the right of the name, and
wraps beneath it rather than squashing when the name is long.

### Size

`index.html` is now 577KB. The console served the previous 506KB in 16-31ms on a home network, so
this remains a non-issue; it will be re-measured after deploy.

---

## [3.59.1] - 2026-09-02 - "Translation corrections" `[VERIFIED]`

A review pass over the six machine-produced locales. Ten corrections, each one a change of MEANING
or of FIT rather than of taste:

- **`gp_badge_mount` in pt, fr and ar** was translated as a past participle - "Montado", "Monte",
  and an Arabic word meaning *installing/fitting*. MOUNT is a CATEGORY badge on a card that is
  explicitly **not** on the console yet, so all three were making the opposite claim. Left as MOUNT.
- **`gp_badge_update` in es** was "Actualizar", the verb. It is a status badge, so it needs the
  noun: "Actualizacion".
- **`ui_install_base` and `gp_btn_install_base_default` in fr** dropped the word "base". The panel
  deliberately distinguishes the base game from an update, and "Installer le jeu" loses it.
- **`ui_filter_addons` and `gp_pill_addons` in fr** were left in English while every other locale
  localised them.
- **Three strings that did not fit their control**: es "Instalar solo la actualizacion" (30 chars
  against 19 on the panel's primary button) and es "Se instala en: el destino por defecto de la
  consola" (50 against 28).

### A verification gap worth recording

`tools/i18n_report.py` checked that every key EXISTS in every language, and reported 100%. It does
not check that a value was actually TRANSLATED, and the review claimed all six locales stopped
partway. Measured against the source: they had not - real coverage is **96-99%** per language
(es 13, pt 19, fr 24, de 23, ja 4, ar 4 strings identical to English out of 614), and of the 69 keys
actually wired to something on screen only **four** matched English, three of them legitimately
("Patches" in pt and de, "Add-ons" in de). So the reviewer overstated the problem and was right
about the specifics - which is why each item above was checked against the source before being
applied rather than taken on trust.

---

## [3.59.0] - 2026-09-02 - "Languages, and a phone that behaves" `[VERIFIED]`

### The panel's Back button also pressed Sleep

Measured, not guessed: with a panel open the panel's Back button occupies `[25,69,102,112]` and the
main page's power button `[61,51,101,91]` - they overlap almost exactly. Hit-testing was already
**correct** (`elementFromPoint` returned `#dback` at both centres and the scrim covered the header),
so this was never a layering fault. It is the touch-screen ghost click: the tap closes the panel and
the click the browser synthesises afterwards lands on whatever is now underneath.

A shield keeps the scrim hit-testable but invisible for 450ms after any panel closes. Verified both
ways: with touch, the point where Back was now resolves to the scrim and dispatching the ghost click
fired the power button **zero** times; without touch the shield never engages, so the PC and the
console are untouched. It is gated on a real `touchstart` event rather than a media query, because
the console reports a coarse pointer in places and must never take that path.

### The queue fell off the screen in portrait

`.dockbody` is centred on the PILL (`left:50%` + `translateX(-50%)`), which is right when the pill is
centred and wrong on a phone where it sits at the left of its own row. Pinned to the viewport
instead, with real safe-area insets. Measured: 10 to 365 of a 375px screen.

### The mobile top bar

239px in five accidental rows, now 193px in four deliberate ones - logo + console status, the queue,
the icon buttons + search, the library summary. Every child gets an explicit `order`, because a
second `max-width:640px` block exists further up the sheet and the result would otherwise be decided
by source position. Measured: zero overflowing elements at 375px.

### Landscape safe areas

The landscape gate had **no** horizontal insets at all, which is why controls sat under the cutout.
Header, sub-bar, main, toasts and the queue all take `env(safe-area-inset-*)` now. The values come
from the device, so a notch, a punch-hole, a curved edge or a plain tablet each get what they need
with no device sniffing anywhere.

### Languages

**494 keys across seven languages, every language at 100%** - `tools/i18n_report.py --check` passes.
`applyI18n` learned `data-i18n-tip` (tooltips) and `data-i18n-aria` (the accessible name of an
icon-only button, which is the only name a screen reader ever gets), so anything added later only
has to carry the attribute. `setLang` now re-runs every path that paints words - grid (FORCED: it
memoises on a signature of the games list, which does not change when only the language does),
queue, drives, source bar, services, and an open game panel rebuilt in place.

**A bug caught before it shipped.** Three elements had been given `data-i18n` whose text JavaScript
rewrites with live values - the connection pill, "Start queue (n)" and "Add-ons (n)". Because
`applyI18n` assigns `textContent`, every language change overwrote the live value with the static
one: the connection pill fell from "PS5 10.0.0.99 - engine not ready" back to "Connecting..." and
stayed there. The rule is that an element JavaScript writes must never carry `data-i18n`; the
translation belongs inside the writer. Verified after the fix: "Iniciar cola (0)", "Complementos (3)"
- translated, and still carrying their counts.

Arabic finally has styling: `applyI18n` had always set `dir="rtl"` and the stylesheet had not one
rule to answer it. Flexbox and text alignment follow `dir` on their own; what does not is anything
positioned with a physical left/right, so exactly those are corrected - the four card corner chips
and the panel's back and close buttons. Mirroring the whole interface is a larger design question
and is deliberately not attempted.

### Honest scope

447 keys are translated and in the file; about 94 are wired to something on screen. The remainder are
strings built inside JavaScript and need `t()` at each call site rather than an HTML attribute. Only
unambiguous matches were wired - 109 candidates with no unique childless element were left alone,
because a wrong `data-i18n` silently replaces real text on every device. The 167 toast strings are
excluded entirely: routing them through `t()` would drop them out of `tools/message_report.py`'s
house-style check, and that gate is not being gutted quietly.

**Size:** `index.html` went from 271KB to 506KB. The companion gzips its responses; the console's C
server does not, so the PS5 receives that raw.

---

## [3.58.0] - 2026-09-02 - "The phone, the queue and the language switch" `[VERIFIED]`

### The phone view did not work, and it was two faults stacked

Measured on the live page at 375x812, not estimated. `body{overflow:hidden}` propagates to the
**viewport**, so the document can never scroll - proven by setting `scrollTop` to 500 and reading
back 0. That leaves `<main>` as the only scroller, and `header` (328px) + `.subbar` (433px) are both
`flex:0 0 auto`, so they took **761px of 812** and left `main` a **51px** content box holding a
7145px grid. On a real iPhone, with Safari's toolbars up, that sliver sits entirely off-screen.
Fixing the viewport unit alone leaves a 52px box; shrinking the chrome alone leaves the bottom under
the toolbar. Both had to go.

| | before | after |
|---|---|---|
| header | 328px | 239px |
| filter bar | 433px | 175px |
| **main content box** | **51px** | **366px** |
| grid | unreachable | 2 columns, scrolls |

The storage block went from a 309px wrapping right-aligned pile to a 56px sideways-scrolling row;
the two `.spacer` divs (about 272px of nothing on a narrow screen) are hidden; the layout is sized
in `svh` so it is never taller than the visible band.

**THE GATES ARE ARITHMETIC ONLY, deliberately not `html:not(.ps5)`.** The console's own page sets
`.ps5` on `<html>` from a **server fact** (`h.on_console`), and a phone browsing
`http://<ps5>:8710` - which is exactly how this app is opened on a phone - receives that class too.
Guarding on it would have switched the entire phone layout off on the one device it exists for.
The two gates are `max-width:640px` and `(min-width:641px) and (max-width:960px) and
(max-height:540px)`; they are disjoint, and neither can match 1920x1080, 3840x2160, 1400x900,
1280x800 or an iPad.

`main` stays the scroller on every device. `virtScroller()` finds it by walking up from `#grid`
testing computed `overflow-y`, and `virtRender()` does `sc.scrollTop - grid.offsetTop` against that
same box - handing the scroll to the document would have silently frozen the virtualized grid.
Only `main`'s height changes.

### The queue could not be opened by tapping it

`#dockhead`'s click handler was a bare `classList.toggle("open")`. `renderQueue()` returns early
while the dock is shut - it will not rebuild rows nobody can see - and `dockShow()` is what adds the
class *and then paints*. On a mouse the `mouseenter` listener had already painted, which hid the
bug completely; on a phone there is no `mouseenter`, so tapping opened an **empty** drawer. The
click path now goes through `dockShow()`.

### The queue bar covered the open game panel

`.dock` was `z-index:60` against `.drawer` 50 **and `.scrim` 40** - so it painted over the artwork
*and stayed live through the scrim*. With `dockShow` bound to `mouseenter` and the console's stick
cursor sweeping continuously, merely crossing the pill popped the 580px queue drawer over the panel.
Now `z-index:30`. Not a guess: the dock was tested at 60, 30, 1 and auto, and its body won the hit
test over a grid card in every case - nothing under it ever needed 60.

### The language switch left text in the old language

Not the `renderGrid` signature memo, which was the obvious suspect and is **not** the cause -
nothing `buildCard()` emits goes through `t()`, so a forced re-render would change no card text.
The real cause was `QLBL_IDLE`: `renderQueue()` cached the dock label's `textContent` on its first
run and never invalidated it, so `setLang()` ran `applyI18n()` to write the translation and then its
own `renderQueue()` call one line later put the boot-language string straight back. That label was
then stuck until the page reloaded - exactly the reported symptom. It now reads the dictionary every
time, and the pill's other states are translated in all seven languages.

### Detail pills

The game panel's top bar now always begins Platform, Region, Title ID; everything after keeps its
previous relative order.

### Honest scope note

Translation coverage is still partial: the file holds roughly 650 user-visible English strings and
only a fraction pass through the dictionary. This release fixes the *mechanism* that made switching
unreliable and translates the queue. Routing the 126 toast strings through `t()` would silently drop
84 of them out of `tools/message_report.py`'s house-style check, so that is a deliberate decision to
take separately rather than a free win.

---

## [3.57.0] - 2026-09-02 - "One fact sheet, two columns, and the whole cover" `[VERIFIED]`

The game panel restructured to the user's sketch. Every action, id and handler is unchanged.

### The details were being said twice

An unlabelled chip row in the header and a labelled "DETAILS" grid below it. Four facts were
duplicated outright (platform, region, size, title id) and two more said the same thing in
different words: for an installed game `whereIs()` and the grid's `locTxt` **both** return
`g.installed_drive`, which is the `INSTALLED TO / LOCATED ON: Internal SSD` pair in the screenshot.

The grid is gone and every fact it carried is a labelled pill in the header, once. Pills size to
their own content — `inline-flex` with no width, so a two-character region gets a two-character
pill — and a value past its cap ellipsizes with the full string in the tooltip, which is exactly
what the grid's `cellTip()` already did for File and Path.

**`.dpill`, not `.chip`.** That class is shared by 19 elements — the settings cheat chips, the
"✓ Installed" badges on the Base/Updates/DLC rows, and `partLabel()`'s inline chip, which sits in a
text line where `max-width` and `text-overflow` do nothing at all. `.chip` is untouched.

Pure CSS, deliberately: `ResizeObserver` and `requestIdleCallback` are **not** in `check_web.py`'s
`KNOWN_GLOBALS`, so the obvious implementation of "panels that resize themselves" aborts both builds.

### ON CONSOLE deleted, without losing anything

Its four facts moved into pills first. `g.backup_path` mattered most: that `📂` line was its **only**
appearance anywhere in the file, and for a ShadowMount backup it is the answer to "where does this
actually live". It is now a Container pill with the full path in its tooltip. `g.console_size` — the
real installed size — now feeds the Size pill.

### The install bar to the left, mods to the right

One token on line 1860. The rescue two lines above stays byte-identical and the bar is **moved as a
node**, never re-authored: `#driveSel`/`#installAll`/`#queueBtn`/`#sendPkg`/`#moreBtn` keep their
parse-time bindings, and `.dcol > .dfoot` is a direct-child rule both columns satisfy.

### The tile IS the switch

A real `<button class="row modtile">`, not a div with `tabindex`. That keeps `disabled` — which is
what enforces all four refusal cases — makes Enter/Space work with no handler of our own, and puts
it in the tab order so the PS5's spatial navigation reaches it. Children are spans; a `<div>` inside
a `<button>` is not valid content.

**The state order is load-bearing.** Both engines set `can_toggle` to the same value as `running`
(`companion/server.py` `"can_toggle": live`), so testing `m.conflict || can_toggle === false` before
`running` — as the first draft did — makes the grey idle state **unreachable** and paints every tile
as a conflict whenever the game is closed. `running` is tested first:

| state | border |
|---|---|
| game not installed or not running | grey |
| running, mod off | red |
| mod on | green |
| conflicts with another mod | amber `#d08a3a` |
| cheat does not match this build | `N/A` |

Conflict is **not** `var(--warn)`: that is byte-identical to `--accent` (`#e6c454`), so conflict and
the focus ring would have been the same colour.

Two columns via a **nested** `.modgrid` wrapper holding only tiles. The compatibility banner, every
`cheatNote()` and "Disable all mods" are `.row` elements too, and gridding the container would have
laid them out as cells beside the tiles. `minmax(168px,1fr)`, never `column-count` — that is recorded
as causing open-time jank on PS5 WebKit.

Patches keep their own Apply button, full width, outside the grid. A patch is written into live game
code and the UI never sends the engine's `unapply`; it must not look like a reversible switch.

### The banner shows the whole cover

`icon0` is a 512×512 **square** and the banner is 1120×160 on the TV, so `object-fit:cover` scales to
the width: the image renders 1120×1120 and a 160px band survives — **14.3%** of the picture, rows
43–57%. That is why a game's logo was never on screen.

Growing the box downward does not fix it, and this was measured rather than assumed: at +96px the
window is still only rows 39–61%, so the logo stays cut. So: two layers. `.artbg` is the old cover
crop, dimmed to .34 and bleeding under the title, doing the one job it was good at — filling the
width with the cover's own colour. `.art` sits on top at its natural square, height-fitted, so
**100%** of the artwork is visible. The panel does not grow by one pixel: `.dbanner` still occupies
exactly 160px and both images are absolutely positioned. Same `src`, so one decode. No blur — it is
the most expensive property on this GPU.

### Verified

Against the patched source on a live server, driving the real `openDrawer()`: no throw; 10 pills;
Details grid gone; ON CONSOLE gone; `.dfoot` in the left column; `#driveSel` populated; two banner
layers; **reopening the panel twice does not throw and `.dfoot` survives** (the regression this
file has shipped before). On Cuphead: one `.modgrid`, 5 tiles, all `<button>`, all `idle` with
"Launch the game first" and all `disabled` — proving the grey state is reachable — grid containing
only tiles with the 3 note rows outside it, and `MODS.rows[i].row === .btn` so `refreshModsQuiet`
patches the right node.

### Known, unchanged from before

`info.running` is read once when the panel opens. Launch a game while it is open and the borders
stay grey until the panel is reopened. A standing poll was designed and rejected: it is not
registered with the page's own arm/disarm, `/api/mods` is not cheap on the console, and
`refreshModsQuiet`'s bare `.catch` would hide a failing chain forever.

---

## [3.56.0] - 2026-08-28 - "A title id is not a name, and the art gets the whole card" `[VERIFIED]`

### Cards that read "CUSA00304"

Reproduced before being touched, against two live machines: Casita showed CUSA00304 / CUSA00579 /
CUSA01412 / CUSA01589 / CUSA07083 / CUSA53974 as the card **name**, while the other PC showed
*Trials Fusion*, *LEGO Batman 3*, *Tony Hawk's Pro Skater 5*, *Dark Souls II*, *Cars 3* and
*LEGO Party* for the same six title ids.

The chain, both halves of which were behaving as written:

`build_library()` appends a console-only entry for a PS4 game that IS installed on the console but
is absent from that PC's folder. The PS5's app.db carries no row for PS4 games on this firmware, so
there is no name to read and it falls back to the title id (`server.py`, `"name": tid`). As a last
resort on a machine that knows nothing else about the game, that is right - an id beats a blank card.

`build_federated_library()` then merges the peers. A peer that *holds the package* advertises the
real name (`pg["name"]` is the game name; the filename lives separately in `items[].file`). When the
title was already present it folded in that peer's base/updates/DLC and even **borrowed its
artwork** - but never touched the name. The card ended up with the peer's package, the peer's cover,
and a title id for a name. `base=1` on the affected entries is the fingerprint of exactly that path.

It also explains why reloading sometimes fixed it: if the console entry had not been built yet on
that pass, the peer path created the game outright and used the peer's name.

Fixed three lines from where the artwork is already borrowed, and in one direction only - a
placeholder is upgraded to a real name, a real name is never overwritten. `is_placeholder_name()`
treats an empty name, a name equal to the title id, and a bare `CUSA/PPSA/PLAS/NPXS/NPUB/NPEB` id as
placeholders; "CUSA Adventures" is not one.

**Verified by reproduction:** a companion configured exactly like Casita - empty library, the other
PC as its peer, the same console - now returns all six real names and **zero** titles displaying an
id, against 6 before.

### The artwork runs the whole card

**No box metric moved.** `.grid` keeps its column widths, `.cover` keeps `padding-top:60%`, the name
row keeps its height. Measured at 375 / 1000 / 1920 / 2400: one distinct card height at every width,
including a card whose name wraps to two lines - so `virtMetrics()` still measures one probe card
and gets the truth.

`object-fit:cover` scales to the larger ratio, so a square 320x320 thumbnail in a 240x144 box was
drawn 240x240 with **40% of the picture cut off** top and bottom - which is what clipped the logos
on Back 4 Blood, Borderlands 4, Crimson Desert and Black Myth. The image is now given the card's
full content height (its own box plus the name row, `--nmh`), so the same source at the same scale
loses only ~17%: **about 83% of a cover survives, against 60%.** It is not upscaled, so not softer.

`.cover` stops clipping and `.card{overflow:hidden}` does it instead, which is what carries the art
into all four rounded corners. The name sits on top through a four-stop gradient - near-solid under
the text, fading to .30 at its top edge so the picture reads through the join rather than meeting a
hard bar (.52 there showed as a visible band across Cuphead and LEGO) - plus a text-shadow so a
title stays legible over pale artwork.

`--nmh` is the name row's real height (2.5em of clamped text + 8px + 9px) and is restated at each of
the three breakpoints where `.nm`'s font-size changes. Measured: the art ends flush with the card's
bottom border at every width. **The chips did not move** - `.cover`'s box is unchanged, so `.tid`
and `.sz` still sit exactly 8px above where the name begins.

---

## [3.55.1] - 2026-08-28 - "Two corrections from the adversarial pass" `[VERIFIED]`

Both found by the review panel on 3.55.0, both verified against the file before being touched.

### Plain `:focus` was sticky on a PC

Click a card, dismiss the drawer with Escape, and that card keeps DOM focus - so its four chips
stayed lit while every other card was blank, which reads as a rendering fault rather than a feature.

The reveal is now `:focus-visible`, which by definition does not match a click-focused
`div[tabindex]` (line 581, `:focus:not(:focus-visible)`, exists to encode exactly that). The reason
3.55.0 used plain `:focus` was the console's stick cursor, which focuses by *clicking* - and that
case is not lost: `.card:hover` covers it, and `.ps5` keeps plain `:focus` as a third path. So the
console now has hover **and** focus-visible **and** focus, and only the PC drops the sticky one.

### The justification comment was false

3.55.0 shipped `.cover .badge.run{opacity:1}` with a comment claiming *".card.running already tints
the border, and the two together are how you find it"*. **`.card.running` (line 167) is dead CSS** -
`buildCard` creates the element as `el("div","card")` and nothing anywhere adds a `running` class;
`isRunning` only chooses the badge. So that border tint has never rendered, and the badge exemption
is not one of two signals but the **only** on-card indication that a game is running - which makes
it more load-bearing than the comment claimed, not less. Comment corrected in place, no CSS change.
The dead rule is left alone: wiring it up would change what a running card looks like, which is a
separate decision.

### A measurement trap worth recording

`getComputedStyle` under Chrome's `--virtual-time-budget` reads a **frozen** transition - the value
never leaves its start point, so an element mid-fade reports `opacity: 0` forever. That made a
working `:focus-visible` reveal look broken in scripted checks while the `.ps5` variant (which has
`transition:none`) reported `1`. Painted screenshots are the honest check for anything transitioned.

---

## [3.55.0] - 2026-08-28 - "The chips step aside" `[VERIFIED]`

The four chips that sit on a card's artwork - platform, status, title id and size - are hidden until
the card is hovered or focused. The artwork gets the whole box back; the facts arrive the moment you
land on one.

### Why this is not just `:hover`

**The PS5 has no single answer, it has two.** The console drives a left-stick cursor (which is why
`.ps5 .card:hover` already existed - *"the cursor sweeps across cards continuously"*) **and** a D-pad
that moves the browser's own spatial navigation onto the `tabindex="0"` cards - the entire X-to-open
path. Both are wired, so neither has to be the one that works.

`:focus`, deliberately **not** `:focus-visible`. The console's cursor mode focuses a card by
*clicking* it, and a click-focused `div[tabindex]` does not match `:focus-visible` - line 581
(`:focus:not(:focus-visible)`) exists to encode exactly that distinction. Keying the reveal on
`:focus-visible` would have worked under the D-pad and failed under the stick.

**Every reveal is its own rule.** One unrecognised selector invalidates an entire comma-separated
list, so pairing a universal selector with a newer one is how you lose both at once. Every selector
shipped here is CSS2/3.

**A phone gets neither** - no hover, and a tap opens the drawer rather than parking focus - so a
coarse pointer that is not the console keeps the chips visible. Being unable to ask for the id and
size at all is worse than a busier card. `html:not(.ps5)` keeps the console out of that branch
whatever pointer type it reports; if the detection ever failed, it fails toward *visible*.

### What was deliberately not done

- **`pointer-events:none`** - the natural-looking way to guarantee the overlay cannot swallow a
  click. It would have silently killed the two tip-carrying badges' tooltips. Not needed: `opacity:0`
  leaves the spans hit-testable and a click on one bubbles to `.card`'s own `onclick`, the same path
  that opened the drawer when they were opaque.
- **`display:none`** - drops the id and size out of the accessibility tree, and zeroes a tooltip
  anchor's rect so the tip parks itself at 0,0.
- **any geometry change** - `virtMetrics()` derives the whole scroll window from ONE probe card's
  measured height. All four chips are `position:absolute` inside a `.cover` whose height is a
  percentage of its own *width*, so opacity cannot move anything.
- **a fade on the console** - these are the same four elements that had `backdrop-filter` stripped
  because a full grid window asked this GPU for ~450 blurs. With a stick sweeping a grid, every card
  crossed would start four more transitions, so `.ps5` gets the reveal instantly instead.

### The one thing that stays up

`.badge.run`. A running game is live state, not a static fact, and there is only ever one of it -
`.card.running` already tints the border and the two together are how you find it in a full grid.

### Verified

Rendered headlessly at 1920x1080 from the **patched file's own stylesheet**, with real library
artwork, in three states - idle, one card focused, and again under `html.ps5`. Idle: all four chips
gone on every card, Running still up. Focused: that card alone shows `PS5 / INSTALLED / PPSA27616 /
1.3 GB`, its neighbours untouched. Card geometry byte-identical between states.


---

## [3.54.0] - 2026-08-28 - "Half-height cards, and drives that follow the console" `[VERIFIED]`

Main page: the storage bar stops describing a console that is switched off, and the game cards lose
just over half their height without losing anything they display.

### Drives for a console that was not there

With the PS5 off, the bar still showed EXT1, Internal SSD, Extended Storage and USB0 — every one of
them reading *"space unknown"*, and the M.2 appearing **twice**. Reproduced exactly against the real
`build_storage()` before being touched: 34 / 51 / 6 / 19 games, the same four tiles as the report.

Two things behind it. `console_apps()` serves its last good list for the **lifetime of the process**
once it has one — past 30s it starts a background refresh and returns the stale list anyway
(`server.py:3603`). That is right for the library, where browsing your games with the console off is
the entire point, and wrong for a storage bar, which is a live readout of what is plugged in. So
`apps is not None` was true forever and the tiles were drawn from a remembered `app.db`, while their
capacity came from `/api/devices`, which correctly fails when the console is asleep.

The empty capacity map also **resurrected the duplicate**. `loc:2` folds into the real extended drive
only `if dst in cap`; with `cap` empty that fold cannot run, so the M.2 came back as EXT1 (its 34
backups, bucketed by container path) *and* as Extended Storage (its 6 packages, bucketed by app.db's
location code). The duplicate was only ever visible offline — which is why it survived the 3.51.0 fix.

Console drives are now gated on `/api/devices` having actually answered. It is a live round-trip
behind the cached TCP probe, so it costs nothing when the console is off, and a console that is up
always answers it with all eleven destinations — `None` means exactly *"did not answer"*. The tiles
come back on their own the moment it does; nothing has to be pressed. PC and peer tiles are untouched.

### Cards at just over half the height

`.grid` is **untouched** — `minmax(172px,1fr)` and every breakpoint override of it — because the
column width was already right on both the PC and the console. Only the height came down, and every
badge, tag, id, size and name still renders exactly as before.

The artwork box was `padding-top:133.33%`, a 3:4 portrait reservation. The source is a **320x320
square** thumbnail (`THUMB_PX`, confirmed live), so that box was already cropping a quarter off each
*side*; `60%` keeps the middle band instead, which is where a game logo sits.

The name row was `min-height:32px` against a two-line clamp. `min-height` is a floor, not a size: two
lines at the TV's 15.5px is 38.75px, so **on the console a card with a wrapping name stood 6.8px
taller than one without** — measured at 1920x1080, seven cards at 372.2px and one at 379.0px. That
matters more than it looks, because `virtMetrics()` measures **one** probe card and computes the
whole scroll window arithmetically from it, so mixed heights make the window drift down a long
library. `height:2.5em` is exactly the two lines it clamps to and follows the font size through every
breakpoint.

Measured, all breakpoints, all cards uniform:

| viewport | card | was | clear artwork between the tag rows |
|---|---|---|---|
| 1920x1080 (console) | 241 x **201.4** | 372.2 / 379.0 | 83.7px |
| 2400 (4K) | 303 x **244.7** | ~458 | ample |
| 598 | 155 x **143.1** | ~290 | 35.7px |
| 378 (phone) | 166 x **147.4** | ~300 | 42.5px |

**Quality goes up, not down.** `object-fit:cover` scales to the larger ratio, so a 300px 4K card used
to ask 400px of height from a 320px thumbnail — upscaling. Measured old/new sampling at 4K:
**1.25 -> 0.94**. Every breakpoint now downsamples, which is the sharp direction.


---

## [3.53.0] - 2026-08-28 - "Three things the 1500px measurement could not see" `[VERIFIED]`

An adversarial review of 3.52.0 found three real defects. All three were confirmed against the file
before being touched, and the third only exists on the device this app is for.

### The About logo was squashed on the console, and only on the console

`.mark{width:44px;height:44px}` lives inside `@media (min-width:1600px)` - the block whose own
comment reads *"PS5 on a 1080p TV"*. **The PS5 renders at 1920px, so the console fires it.** The new
`.scard h3 .mark` rule declared no width or height, so the old one was unopposed: the span became
44x44, `max-width:100%` then capped the image to 44px, and `overflow:hidden` clipped what was left.

`class="mark"` appears **exactly once** in the file - on the About logo added in 3.49.0. The page
header it was originally written for uses `.brandlogo`. So the rule had one consumer left and it was
squashing it. Removed, and the settings rule now states `width:auto;height:auto` so the next one
cannot creep back in.

Measured at 1920x1080: the wordmark renders **63x22**, its natural shape, inside a card ending 16px
to its right. Every earlier measurement was taken at 1500px, which is below the threshold - the bug
was never in view.

### Two buttons renamed themselves after one press

Moving the actions into the card headers meant shortening two labels, and the handlers still restored
the originals: `#testConsole` markup said **Test** and its handler wrote back **Test connection**;
`#cheatRescan` said **Rescan** and wrote back **Rescan cheats**. So each read correctly until first
use and wrongly for the rest of the session - and in Your PS5, the one card with three header
buttons, that is enough to wrap the row permanently.

`#devScan` does this correctly (markup and handler both say "Find PCs"), which is what shows these
two were an oversight rather than a decision. Verified by pressing both: labels unchanged, no header
row wrapped.

### Also

- **The inbox path was rendering proportional.** `mono` and `ell` are not standalone rules in this
  file - they only work through a parent like `.svc .v`. On a plain `.hint` div they did nothing, so
  the one path a user has to type by hand was in the body font. It now sets `var(--mono)` directly,
  and `word-break:break-all` so a long path wraps inside the card instead of straining it.
- **The fleet list is hidden on `!== 1` rather than `> 1`.** With `> 1` a console count of *zero*
  also hid it, taking the "none configured" message with it. It is only redundant when there is
  exactly one console to name.

**48 checks, all passed.**

---

## [3.52.0] - 2026-08-28 - "Settings, second pass: the panel stops scrolling" `[VERIFIED]`

Six changes from a marked-up screenshot, plus one the markup made obvious.

### The dead space between rows is gone, and so is the scrollbar

The cards were a CSS **grid**, and a grid stretches every row to its tallest card - so a short card
left a band of empty space before the next row started. That is the gap the user drew a box around,
twice. The cards flow in **columns** now (`column-count` with `break-inside:avoid`), so each one ends
where it ends and the next starts immediately below it.

Measured: largest gap between stacked cards is **13px** - exactly the intended margin - and the panel
**no longer scrolls at all** at 1500x1000. Reflow verified at 1500 / 1000 / 700 / 430 px: 3 -> 2 -> 1
columns, nothing overflowing at any width.

### A card's actions moved up beside its title

`Your PS5` (Find it / Test / Open on PS5), `Cheats & mods` (Rescan), `Other devices` (Find PCs) and
`Advanced` (Clear state / Install log). Each card's `<h3>` is now wrapped in a `.chead` flex row with
its buttons on the right - **siblings of the heading, not inside it**, so the heading still reads as
a heading.

**Power & helpers was deliberately left out.** Its button stops every homebrew payload on the console
the instant it is pressed, and a header is where you put the things that are safe to hit by accident.

### And the rest

- **About stopped spanning two columns.** At 750px wide its four label/value pairs were so far apart
  the user drew leader lines between them. It is 372px now, the same as every other card.
- **The logo was hanging out of its card**, over the one beside it. Height-capped, allowed to
  shrink, clipped to the box: right edge 1305 inside a card ending at 1321.
- **The address box was full width for fifteen characters.** 190px now.
- **"PS5 - 10.0.0.99" is gone.** The Connection row two lines above it already said that. The fleet
  list stays in the DOM and reappears the moment there is more than one console to tell apart.
- **Inbox is a chip again**, like the four beside it. It had been a printed row because a chip keeps
  its folder in a tooltip and `.tipbox` is `display:none` under `pointer:coarse` - so the inbox path
  is now *also* printed under the chips, where a phone can read it. Verified visible at 430px.

### One the screenshot could not show

Header buttons measure **51x23px**. That is fine for a mouse and fine for a controller - which
focuses rather than aims - but under half of what a finger needs, and this file already sets a 44px
floor for buttons elsewhere under `pointer:coarse`. They get `min-height:38px` and real padding on
touch.

**48 checks, all passed.** 63/63 ids, every handler bound, six LEDs green, no empty rows, eight
translatable titles, and `restOff` confirmed absent from every header.

---

## [3.50.0] - 2026-08-27 - "The cheat folders were unreachable on the console" `[VERIFIED]`

Two defects in yesterday's Settings redesign, both found by an audit of the page as just shipped.

### The chips hid their paths from the only devices that needed them

The Cheats card exists to tell you where to put files. 3.49.0 turned the five folders into chips and
moved every path into `data-tip` - with a source comment claiming the tooltip was *"still reachable
with a controller"*. That was wrong, and both escape routes were closed:

- `index.html:500` - `@media (pointer:coarse){ .tipbox{display:none!important} }`. Tooltips do not
  exist on touch, which is the phone the Devices card invites you to open.
- The tip driver's other route is `focusin` (`index.html:3680`), and the chips were bare `<span>`
  elements. **A span with no `tabindex` never receives focus**, so that path could never fire.

Split by what you actually do with each path. The **inbox** is the one you type, so it is a printed
row again (`.v.mono.ell`, which truncates from the left - the correct end for a path). The four
format folders are destinations you never type, so they stay chips but gained `tabindex="0"` and a
focus ring, which makes them reachable with a D-pad.

### The wordmark was 81px left of centre

`.sheet h2{flex:1}` centres the title inside its own box, and that box sat between a narrow left
cluster (Back) and a much wider right one (language, save, close). Measured: **81px off**. The header
is a `[1fr auto 1fr]` grid now, which is the only arrangement that holds the centre however wide the
side clusters get. Measured after: **-5px**, and it no longer moves when the save status flashes.

*(An audit lens also claimed the title shifted whenever `saveState` flashed. Measured at 81px both
with and without the longest status string - refuted, and the fix landed anyway on the real defect.)*

**48 checks, all passed.** 63/63 ids, every handler bound, all six LEDs green, live data in every row.

---

## [3.49.0] - 2026-08-27 - "Settings: one page of cards" `[VERIFIED]`

### Eight tabs become one page

The tabs were the reason the page was hard to use: the answer to *"why will this not install"* was
always behind a tab you were not on. Settings is now a single grid of eight cards - **Your PS5,
Installing, Your games, Cheats & mods, Other devices, Power & helpers, About, Advanced** - each with
its own icon and heading, reflowing 3 columns to 2 to 1 as the panel narrows. Verified at 1500 /
1000 / 700 / 430 px: no card overflows and the panel never scrolls sideways.

Every one of the **63 controls kept its id**, and that is not tidiness. Handlers here are bound
**once at parse time**, so a renamed id is a button that silently stops working - and two are worse
than silent: `openSettings()` reads `setIp` and `setPort` **unguarded**, so losing either would throw
before `_showSettings(true)` and the panel would never open at all.

### What the redesign changed beyond the layout

- **A plain connection line.** The card now opens with a lit dot and *"Connected to 10.0.0.99"*.
  There was no such row anywhere - you had to read the engine block and infer it.
- **The cheat folders became chips.** Five rows of long Windows paths told you nothing at a glance.
  The chips say which formats are handled; the folder moves into the tooltip, which the PS5 draws on
  focus as well as hover, so a controller can still reach it.
- **A controller footer** - X Select / O Back / OPTIONS Help - drawn only on the console, keyed off
  the `.ps5` class the app already sets. No JS has to remember to toggle it.
- **The rest-mode explanation was printed twice**, verbatim, ten pixels apart. Once now.
- **`restOff` stays a red-outlined button on its own row.** It stops every homebrew payload on the
  console the moment it is pressed; the mockup's slot for it looked like a toggle, and a destructive
  action must not be able to read as a preference.

### Removed

`setTabs` / `.settab` / `showSetTab()` / `_setTab` - the whole tab mechanism. `engineName`, which had
no JS reference at all. `fixHosts` + `hostsNote` + `cleanupHosts()` - an inline-hidden button whose
only caller was its own handler, calling `/api/dpi/reload`, which the console does not implement.

### A translation bug, found and fixed on the way

`applyI18n()` assigns **textContent**, which has two consequences the redesign had to respect. A
title carrying `data-i18n` directly would have **wiped its own icon**, so the key sits on an inner
span. And `&amp;` in a dictionary value renders as those five literal characters - *"Cheats &amp;
mods"* had been showing exactly that. Four values across the language files are fixed.

Verified across all seven languages: titles translate (`Tu PS5`, `Vos jeux`, `Energía y servicios`),
all eight icons survive the switch, and no raw entities remain.

### Proven, not assumed

63/63 ids present · every handler still bound · all five saved settings round-trip through
`POST /api/config` and back **with the original values restored afterwards** · every status row
populated from live data with all six LEDs green · `consoleNote` appears and the three PC-only
buttons disable under a simulated console · the install log fetches and renders.

`tools/ready_check.py` gained checks for the new shape - card grid, eight cards, no tab machinery,
all eight titles translatable, all five saved controls present - and **stopped depending on FTP** for
its transfer check. `ftpsrv` is a separate payload the user may not have loaded; it was not on this
run, and a passing transfer was reported as a failure while its 227 MB probe was left behind, because
the cleanup needed FTP too. It uses our own file API now, which is by definition running.

**48 checks, all passed.** Nothing outside the Settings panel was touched.

---

## [3.48.0] - 2026-08-26 - "One drive, one tile, four facts" `[VERIFIED]`

### The M.2 appeared twice

Two tiles both read **Extended Storage**: one with 33 backups (1557 GB), one with 6 games (44 GB).
They are the same physical drive. A title with a container is bucketed by where that container
really is (`ext1`); a title without one falls back to app.db's location code, and code `2` *is* the
extended drive. Nothing merged them. The app.db buckets now fold into the real drive they name -
`0` into internal, `2` into whichever extended drive is present.

### 790.9 GB of games on a 673.9 GB drive

Internal claimed more used space than the drive physically has. The figure was the sum of each
title's **app.db size**, and app.db's size is metadata - this project already records it as bogus
and reads real container sizes wherever it can. It overshot by 138 GB and said so on screen.

When the console gives us real `statvfs` numbers, used is now `total - free`: what the filesystem
says is occupied. The per-title sum survives only as the fallback where nothing can be measured.

    before   Internal SSD   790.9 GB used   of 673.9 GB     <- impossible
    after    Internal SSD   652.8 GB used   of 673.9 GB     21.1 GB free   51 games

### Every tile said something different

A console drive showed free space. A PC showed a game count. One PC also showed its IP address. Read
side by side they looked like different kinds of thing. Every tile now carries the same four facts -
**device, what its games occupy, free of total, how many games** - and no addresses: a tile says what
a device holds, not how to reach it. PCs report the capacity of the drive their library actually sits
on, which they always knew and never sent.

### "Casita (this PC)" - on the PS5

`local` was set by whichever **companion answered**, which is the wrong question. On the console the
page is served by the PS5 and answered by a PC, so the console's screen announced a PC in another
room as "this PC". The browser is the only thing that knows where it is being looked at, so it
decides now: on the console, no PC is "this PC". Verified by driving the real page - the marker
appears exactly once on a PC and disappears entirely under a PlayStation user agent.

### The Devices list had the same split personality

Peers got an indicator, a title count and a PS4/PS5 breakdown; **this PC** got a bare line with
none of them. Both are written the same way now - indicator, name, titles, PS4/PS5 split, OS - so
the list reads as one list. The address stays *here*, in Settings, where knowing it is the point.

**Note:** a peer only reports its drive capacity from 3.48.0 onward, so Casita shows *"space
unknown"* until it is updated. Nothing else about it is affected.

---

## [3.47.0] — 2026-08-26 · "Audit pass: the tool that could crash the console, and five things it hid" `[VERIFIED]`

A full six-lens audit of everything this session touched, every load-bearing finding checked a second
time by an agent whose job was to refute it. Ten fixes landed; a dozen further claims were refuted on
re-check and deliberately left alone.

### The worst finding was in our own tooling

`tools/ready_check.py` wrote a 4 KB zero-filled **`.ffpfsc` straight into `/mnt/ext1/homebrew`** —
a ShadowMount **watch folder**. ShadowMount mounts whatever appears there the instant it appears,
and a 4 KB file named like a compressed PFS container is exactly the partial-mount crasher this
project documented long ago. It ran on **every** non-`--offline` invocation, by default, unprompted.

It is now behind `--test-delete`, and when it does run it writes `<name>.ffpfsc.part` and renames
only once the bytes are down — the same `.part` convention every other writer here already uses, for
exactly this reason.

### Two gates in that tool passed when they had found nothing

- *"all three carry the same version"* — if all three regexes missed, every value was `"?"`, and
  three unknowns are equal, so it went **green**.
- *"both sides are the same version"* — with neither server answering, `None == None` was **green**.

A readiness tool that reports success on absence is worse than no tool.

### PS4 games on extended storage were invisible

The detector rewritten in 3.43 scans **one** root, `/user/app`. Every other place in this project
that asks the same question scans four, because a PS4 game installed to extended storage lives under
`/mnt/ext1/user/app`. With Installation Location now set to the M.2, **every game installed from here
on would have read as not-installed** — and `/api/install` refuses an update whose base is missing,
so their patches would have been rejected with `base_not_installed`.

It scans all four now, and reports which drive each game is actually on instead of asserting
Internal SSD for every one.

### A failed read was being written to disk as a fact

In the same scan, *"I could not read this folder"* and *"this folder is empty"* were the same answer.
That is not cosmetic: a title dropped there falls out of `console_apps()` → `installed_titles()` →
`/api/installed` → `prune_local_installed()`, which **rewrites `installed.json` without it**. Nothing
ever puts it back. A failed read now raises, and the existing handler returns the previous cache.

### One unlucky TCP connect could abandon a healthy install

`install_spawn()`'s verdict poll allows three consecutive transport failures before declaring the
console gone — but `up()` caches a negative for 8 seconds and the loop sleeps 2. So a single failed
`connect_ex` was re-served from cache twice and spent all three strikes in about **four seconds**, on
an install running perfectly. The message it produced is the one that invites a mid-install ELF
reload — which is precisely how the duplicate-install crash happened. A strike now requires a forced
fresh probe; the offline-speed caching is otherwise untouched.

### The error table exists in three files and only one was updated

`install_error_text()` lives in `companion/server.py`, `server.c` and `installer_probe.c`, and each
one's own comment says *"change one, change all three"*. 3.44 changed one. So an out-of-space failure
told the user *"the package may be incomplete, so download it again"* — on an 85 GB file, the single
action that cannot help — everywhere except the console's own copy. All three agree again.

### Four UI defects

- **The patch-only branch never repaired the button's class.** `installItem()` writes `"btn done"` on
  success, dropping `primary`; the main branch repairs it, this one did not — so a patch-only title
  opened after any successful install showed a finished-looking green button.
- **The busy label was inverted**: Install said *"Queued…"*, ＋ Queue said *"Adding…"*. And every
  restore wrote the literal `"Install"` back onto whichever button had been borrowed — relabelling
  the ＋ Queue button, and a PS5 backup's *"Mount backup"* footer, on any cancel or refusal. The
  resting label is captured and restored now.
- **`/api/engine/state` does not exist on the console**, which answers unknown `/api/` GETs with a
  bare `{}`. `{}` is truthy and `e.ok` is `undefined`, not `false`, so the guard passed and the
  Settings panel painted *engine: no*, *shop: not answering*, every LED dark — on a console where the
  engine was working. That is the panel someone reads immediately before deciding to reload the ELF
  mid-install.
- **A title can be both a PKG and a backup.** `isBackupTitle()` answers about the *title*, which is
  right for the MOUNT badge and for Move; the install footer needs to know about the *file*, because
  the server routes on the file's extension. New `installsAsBackup()` for the four decision points
  that make a user-facing promise. (Zero titles differ in the current library — this guards the case
  rather than fixing a visible symptom.)

### Also

- A failed `gen_web_bundle.py` only **warned**, and the build then linked the previous
  `web_bundle.h` — shipping a console UI older than `web/index.html` with nothing to say so. It
  aborts now, and `ready_check --offline` asserts `bundle == disk` as the backstop. That assertion
  caught this release's own stale bundle during testing, exactly as intended.
- The space preflight no longer applies to **add-ons**: a patch is small, the console already
  committed the space for its game, and there is no force button for `no_space` in the UI.
- `check_web.py` now rejects **calls to functions defined nowhere** and the two-argument
  `classList.toggle`. Proven by re-injecting both bugs: caught at their exact lines, build stopped.
  This is the gate that would have caught `T()` instead of `t()`.
- `renderQueue()` had a live two-argument `classList.toggle("busy", …)` — pre-existing, and the file's
  own comment 600 lines below warns against it.

### Deliberately not done

`sceAppInstUtilCancelInstall` is not coming back in any form. Calling it with an inferred signature
took the console down, and "a spawned process contains the crash" was wrong reasoning: it acts on
shared ShellCore state. A stalled download is named plainly and the user is pointed at the PS5's own
Downloads list, which clears it safely. Describing a manual step is not a defeat.

---

## [3.45.0] — 2026-08-26 · "Stop guessing where the console installs" `[VERIFIED]`

### The space check blocked a working path — reverted to something that cannot

3.44.0 refused an install when the target drive looked too small, and worked out the target by
looking at where the console **last** put a package. That inference goes stale the moment the setting
changes — which is precisely the user's normal workflow: change **Installation Location** on the PS5
by hand, then install. They had just switched to Extended Storage (380 GB free) and the app still
insisted there was no room, because its guess still said internal.

**Where a PKG lands is the console's decision, and the app has no business predicting it.** The check
no longer tries. It answers only the question that needs no inference — *is there anywhere on this
console it could possibly go?* — and stays silent otherwise:

- fits on **any** connected drive → goes through, the console decides where, as it always did
- fits on **none** of them → refused, naming the largest free space there is
- console will not answer → says nothing at all

A preflight that guesses is worse than no preflight: it blocks work that would have succeeded.

**Verified:** RDR2 (85.3 GB) with internal at 21.2 GB and ext1 at 380.3 GB now queues normally,
where 3.44.0 refused it.

### The Queue button never came back

`installItem()` reports its result by writing onto the button it was handed — `"＋ In queue"`,
`className = "btn done"` — and the drawer's footer buttons are reused by every game. Only the
primary button's *text* was ever reset, so after a single queue press the Queue button read
"＋ In queue", styled as finished, **for every game opened afterwards**: two buttons that both looked
like Install and no way left to queue anything. `installAll` had the matching half — its text was
restored but its `className` was not, so it silently lost `primary` and stayed grey.

Both are now restored to their resting state every time the drawer opens.

### The footer says Install and Add to queue

"Install base" / "Reinstall base" / "Install latest update" are all just **Install** now. Which file
that means is on the panel above and spelled out in the button's tooltip; the button says what
pressing it does. A PS5 backup keeps **Mount backup** / **Re-send backup** — it is mounted, not
installed, and that distinction earns its words.

**Verified in the browser against the shipped exe**: three consecutive drawer opens, each deliberately
dirtied in between the way a real press does, every one coming back to `Install` + `＋ Queue` with
both visible — and a PS5 backup still reading `Re-send backup`.

### Note

`check_web.py` parses the UI script but cannot catch an undefined reference — a first cut of this
change called a translation helper that does not exist (`T()` instead of `t()`), which parsed fine
and threw at runtime on every drawer open. It was caught by driving the real page in a browser.
Syntax passing is not the same as the page working.

---

## [3.44.0] — 2026-08-26 · "Out of room, said plainly" `[VERIFIED]`

### "Some games install, others fail" was the console running out of space

Red Dead Redemption 2 (85.29 GB) failed with `0x80B21104`, and the app said:

> The console's installer refused it — the package may be incomplete, so download it again

The package was not incomplete. `/api/verify` reads *"complete (pfs image fits + tail high-entropy),
confident"*. What was actually true at that moment:

    internal  free  20.1 GB   total 673.9 GB      <- where PKG installs were landing
    ext1      free 380.3 GB
    usb0      free 204.9 GB

and `bgft.db` has **no row at all** for `CUSA03041` — it was refused before the transfer began,
which is what "not enough room to start" looks like. The 17 games in the same queue that worked were
the ones that fit; ~446 GB had just gone onto internal in one sitting (Mafia III 46 GB, Dragon Age
44 GB, Black Ops III 43.6 GB, all `status 1036`). That is exactly *"some work, some don't"* — and it
had nothing to do with which games "always worked before".

**It refuses before starting now, with the numbers:**

> Not enough room on the PS5. Red Dead Redemption 2 needs 79.43 GB and Internal SSD has only
> 18.68 GB free. Extended Storage has 354.22 GB free — change Installation Location on the PS5
> (Settings › Storage) to install there.

The hard part was knowing *which* drive a PKG lands on: Sony's installer takes that from the
console's own Installation Location setting and no API reports it — and guessing a registry key is
a rule this project already has for good reason. It does not need the setting. It reads the
**outcome**: the drive the console last actually put a package on. That is evidence, and it is right
for the only case that matters — the drive that is filling up is the drive in use. If the console
will not say how much room it has, the check says nothing and stays out of the way; `force` still
overrides.

`0x80B21104` is also named now instead of falling into the generic `0x80B2xxxx` bucket, whose
sentence sent the user off to re-download an 85 GB file — the one action guaranteed not to help.

### Queue buttons had to be pressed twice

`renderQueue()` did `body.innerHTML=""` and rebuilt every row from scratch, **every 1.2 seconds**
while anything was installing. A press is not instantaneous — there is a gap between the button
going down and the click being delivered — and any repaint inside that gap replaces the element the
press started on, so the click lands on nothing. With eighteen rows in the dock that is a lot of
chances to miss, and a controller press is slower than a mouse click, so it missed more often.

Rows are now **built once and updated in place**: only the status line, the bar and the retry
button's visibility change, and the button elements survive from one repaint to the next.

**Verified in the browser**: the same button object survives 5 consecutive renders (it did not
before), and a cancel click landed and removed its row *while renders were firing every 120 ms* —
the exact condition that used to swallow presses. It is also far less work for the PS5's browser: an
idle queue now costs nothing per tick instead of a full rebuild.

---

## [3.43.0] — 2026-08-26 · "Artwork is not a game" `[VERIFIED]`

### Fifty-three PS4 games the console did not have were shown as installed

After a **reset database** on the PS5, the console reported 2 PS4 games installed. The app reported
55. Everything downstream of that was wrong, and it was all one bad signal:

    "PS4 games the PS5 app.db drops are still really installed:
     they keep /user/appmeta/<CUSA>/icon0.png"

That was verified in July and was true then. But it is not a test of *installation* — it is a test of
whether the console has ever heard of the title. **Artwork outlives the game.** Measured here:
`/user/appmeta` holds 55 CUSA folders, and ARK (not installed) has an 8.2 MB `icon0.png`
indistinguishable from Riptide's (installed).

The real test is the game's own payload, which a reset *does* remove:

| | folders | with a non-empty `app.pkg` |
|---|---|---|
| `/user/appmeta` | 55 | — (artwork only) |
| `/user/app` | 31 | **2** — `CUSA02365` (107 MB), `CUSA58072` (738 MB) |

Two of thirty-one, matching the console's own count exactly. The other 29 `/user/app` folders are
**empty shells** left by the reset — so the folder is not enough either; the payload is. Detection now
requires `/user/app/<CUSA>/app.pkg` with bytes in it, and returns that size, which is the only real
figure anyone has for these titles (app.db has no row for them).

**One signal, four symptoms** — all fixed by correcting it:

- games shown as installed that were not
- **no size** on those rows, because there was nothing real to measure
- **Install offering the UPDATE instead of the base game** — a title believed installed has only
  updates left to apply (`stateOf()` returns `"upd"`)
- **progress stuck at 0%** — an update whose base is not there cannot make progress

Verified after the fix: 2 CUSA installed, matching the console. ARK, Bloodborne, Cuphead and Tony
Hawk now read *not installed* with real sizes (15.7 / 31.4 / 3.6 / 19.9 GB) and Install picks the
base. A real install of Cuphead ran `6% → 12% → … → 88% → 100%` with live byte counts.

### The storage tiles claimed 3352 GB on a 2 TB drive

    before:  Internal SSD      used 1.3 GB     free -  total -   3 games
             Extended Storage  used 3352 GB    free -  total -  52 games

Three faults in `build_storage()`:

1. **It bucketed by app.db's location code**, which has two values — 0 "Internal" and 2 "Extended" —
   so every external drive collapsed into one tile. This console has two: ext1 holds 33 backups,
   usb0 holds 19. Their sizes were added together. The tile was never a drive; it was
   "everything not internal".
2. **`free` and `total` were always `null`**, so a tile could only show a used figure with nothing to
   judge it against — and nothing to contradict it when it went wrong. The console has had the real
   numbers all along: `/api/devices` runs `statvfs` per drive.
3. **A backup's drive is where its container is**, which app.db does not know. `backup_path` does.

    after:   Internal SSD      used    1.3 GB   free  582.8 GB   total  673.9 GB    3 games
             Extended Storage  used 1557.2 GB   free   71.2 GB   total 2000.4 GB   33 games
             USB0              used 1794.7 GB   free  204.9 GB   total 2000.4 GB   19 games

USB0 now reads 1794.7 used + 204.9 free = 2000 GB, which is a 2 TB drive.

---

## [3.42.0] — 2026-08-26 · "The app stops pretending a PC is your console" `[VERIFIED]`

### It adopted a peer PC as the PS5 — and wrote it to config

Found with the console switched off: `config.json`'s `ps5_ip` changed itself from `10.0.0.99` to
`10.0.0.72` — the address of *Casita*, another PC on the network running PKG MUTANT SHOP — and the
change was **saved to disk**, so it would have outlived the console coming back. The app would have
kept reporting the console offline while quietly talking to a PC.

`discover_ps5()` accepted any host with `8710`, `8084` or `2121` open. Every PC running the companion
has 8710 open and answers the same API, so a peer and a console are indistinguishable at the port
level. `main()` then took `found[0]` — lowest address wins, which is arbitrary — and persisted it.

**An open port proves a host is running something, not what it is.** Discovery now asks the host
itself: our on-console ELF reports `on_console: true` / `server: "on-console"` and the companion does
not. A host that identifies as a PC is never a candidate, whatever ports it has open. If it will not
answer at all, it must have **8084** (Payload Manager) open — a PS5-only fingerprint — otherwise it
is not considered, which stops a slow-to-answer peer from passing as a busy console. A confirmed
console always outranks an unconfirmed one, and **only a confirmed console is written to disk**: a
guess is good enough to try for one run and must not outlive it.

Verified with the console off: config stays `10.0.0.99`, health reports `ps5_ip=10.0.0.99,
connected=false`, and nothing is adopted.

### With the console off, the app was unusable

Every call waited out a full HTTP timeout against a machine that was not there. The UI polls health
every 6 seconds, so the whole app sat grey — exactly when someone would open it to find out why.

| | before | after |
|---|---|---|
| `/api/health` | **10.4 s** | **0.00 s** |
| `/api/library` | 20.0 s | 0.00 s |
| `/api/devices` | 8.0 s | 0.00 s |
| `/api/move/status` | 10.0 s | 0.00 s |

A TCP connect settles "is it there" in milliseconds; an HTTP request waits out its whole timeout.
`Ps5Bridge.up()` probes the shop port with a 0.6 s budget and caches the verdict briefly — 4 s for a
yes, 8 s for a no, so a console that is reloaded reappears almost at once. Every console-bound call
asks first: the proxies (`_shop`), the file API, the health probes, and `helper_status()`.

Deliberately one-way: unknown is treated as "try it", so the gate can only ever skip a call that was
going to fail. No install, delete or transfer behaviour changes.

### `tools/ready_check.py`

One command that answers "is everything actually working?" — `--offline` for what can be checked with
no PS5, otherwise it waits for the console and checks the lot. Every check names the bug it guards
against: versions matching across all three artifacts, the embedded web bundle matching
`web/index.html`, that **only `isBackupTitle()` reads `lane==="mount"`**, both servers serving delete
and move, the queue verbs, the delete boundary, the auto-clean state, and a real transfer to a chosen
drive. It never installs a package and never deletes one of your games.

Written after re-establishing the same facts by hand five times in one session.

### Note on the repo's own config

`companion/config.json` was pointing at `10.0.0.72`. It is a development file — the shipped exe reads
the `config.json` beside it, which had the correct address throughout — but it is what made the
discovery bug visible, and it is now correct.

---

## [3.41.0] — 2026-08-26 · "Pick the drive again — and only the ones that are plugged in" `[VERIFIED]`

### PS5 backups can be sent to any drive again

The destination selector had gone dead for every PS5 game: whatever you chose, it read *"Installs
to: Extended Storage"* and could not be changed.

The gate was one line — `var isMount = g.lane === "mount";` — and `lane` is not a reliable way to
ask whether a title is a backup. A title the console reports as installed is rebuilt with `lane`
hardcoded to `"installed"` (`companion/server.py:257`), which throws away the fact that it is a
ShadowMount backup. **Zero of the 112 titles in the live library carry `lane:"mount"`**, on either
server — so the test was false for all 52 backups, they fell into the *"a reinstall must land where
the game already is"* branch, and the select was disabled.

`isBackupTitle()` replaces it and reads the signals that actually survive — `source:"backup"`,
`backup_path`, a container `format`, or `base[0].kind` — any one of which is enough. Replayed over
the live library from both servers: **52 backups matched, 0 PS4 packages misidentified, 0 missed.**

Nothing about the routing needed fixing. `drive → mount_dest_for_drive() → dest → fetch_to() →
/api/engine/fetch` was intact the whole time; only the control was missing. **Verified on hardware:**
Evergate (1.98 GB) sent to **usb0** — not the ext1 default — landed in `/mnt/usb0/homebrew`, mounted,
playable, 100 s. A second 107 MB fetch to usb0 confirmed the lane independently.

PS4 and PS5 **packages** are unchanged: Sony's installer takes its destination from the console's own
Installation Location setting, so those still read "Installs to: console default".

### Only drives that are actually plugged in

The picker offered eleven destinations — internal, ext0, ext1 and usb0–usb7 — on a console that has
three. The console has always known better: `/api/devices` `stat()`s each path and compares `st_dev`
against `/mnt`'s own, so an empty placeholder folder is never mistaken for a mounted stick, then
requires `statvfs` to return a real capacity. That `detected` flag was simply ignored.

Now: **3 offered, 8 hidden.** Each shows its real free space, a drive too small for the game says
*"not enough room"* rather than failing on the console minutes later, and the default is the drive
the container is really on — read from `backup_path`, not from `installed_drive`, which reports where
app.db has the title registered and disagrees with where the file sits (app.db says 52 titles are on
"Extended Storage"; the containers are 33 on ext1 and 19 on usb0).

If the console cannot be reached, every destination is listed again rather than none, and the tooltip
says why.

### An installed backup could not be moved at all

"Move to another drive" needs `movable` **and** a path. The removable-stick scan sent both; the
app.db row — how *every* already-installed backup is reported — sent `backup_path` and neither of the
others. So the one control that puts a game on a different drive was hidden for exactly the games
that have a container to move, and with no PC on the network an installed backup had no route to
another drive at all: no Install (its `base` is empty on the console) and no Move.

Both servers now send `movable`, `local_path` and `runs_in_place` for a title with a container.
All 52 backups carry them.

This also fixed a message that would have been wrong: the new *"you already have a copy on ext1"*
warning points at "Move to another drive", which was not in the menu. Making the control exist beats
rewording the sentence.

### Settings: one column, and the tab bar crosses the panel

The tabs made two columns redundant, and the layout fought them: `.setbody` was a **grid** and the
tab strip was one of its **grid items**, so the bar could only ever be as wide as the first column
with pane content sitting beside it rather than under it. `display:block` fixes both halves — the
strip becomes full width, the panes stack beneath it. A negative margin cancels the body's own 20 px
padding so the strip's rule runs the whole width of the panel, and the sheet narrows from 1040 px to
880 px, since 1040 was the width of two columns.

### Smaller things

- **Choosing a drive on a console-served page with no PC now says so.** The console's own
  `/api/install` never reads a destination and has no mount lane, so offering the choice there was
  offering one that could not be honoured. It points at "Move to another drive", which *is*
  drive-aware on the console and works with no PC at all.
- One predicate replaced five ad-hoc backup tests scattered through the UI (the badge, the install
  button label, the delete menu item, the delete itself, clean reinstall), which had drifted apart —
  the delete already used a broader test than the picker did, which is why deleting worked while
  choosing a drive did not.
- **The version stamper's warning became a stamp** in 3.40.0; this release is the first where the
  ELF and the companion carried the same number automatically.

---

## [3.40.0] — 2026-08-26 · "Delete works, and the controls only a PC can do say so" `[VERIFIED]`

### Deleting a PS5 game finally removes it

Deleting a backup from the PS5 home screen was never enough: the container stayed in the drive's
homebrew folder, ShadowMount saw it on the next scan, and the title came back. The only real fix was
FTP-ing in by hand. The Actions menu had carried a **Delete from PS5** item for months that answered
*"on-console delete isn't enabled yet"* and stopped there.

It removes the container now, on the console, and **nothing on the PC is touched** — the app sends a
title id, never a path, and the console does the lookup in its own scan. A PS4 package is left alone
on purpose: it removes itself completely from the dashboard, so the app says that instead of offering
a button that would do less.

**Verified on hardware:** a container at a drive root deleted (2 048 B, path echoed back); a nested
folder dump deleted whole (3 levels, 24 576 B of contents), with the unrelated file beside it
untouched and the watch folder's other 34 entries intact.

### Four bugs in that delete, all mine, all found before the user hit them

- **The "refuse while a game is running" guard never fired — in either state.** `running_game()`
  returns `0` on success; written as a bare truth test the `&&` short-circuited exactly when a game
  *was* running. Every other call site in the file gets this right. Pulling a container out from
  under a mounted, running game corrupts a save and hangs the shell.
- **A folder-shaped backup could be found but never deleted.** The removal was `unlink()` with an
  `rmdir()` fallback, and `rmdir` only takes an *empty* directory — so it failed with `ENOTEMPTY` and
  blamed the drive. Folder dumps get a bounded recursive delete now: homebrew roots only, re-checked
  per subdirectory, `lstat` so a symlink is removed rather than followed, depth-limited.
- **The delete was unreachable from the console-served page** — the no-PC case the feature exists
  for. `POST /api/game/delete` was on the companion only, and the console answers unknown `/api/`
  paths with `200 {}`, so the button would have done nothing at all. The console serves it now by
  forwarding to the one implementation rather than growing a second.
- **Drive roots were folded into the homebrew list.** ShadowMount scans a drive's root as well as its
  homebrew folder, so a container left at `/mnt/usb0/` has to be reachable — but a drive root is not
  a folder that exists to hold games. This console's `/mnt/usb0` also holds *PS5 Xplorer*,
  *InternetBrowser*, `shadowmountplus.elf` and Sony's own `PS5` folder. The roots are two tiers now:
  homebrew folders take containers *and* folder dumps; drive roots take containers only, regular
  files only, never a directory whatever is inside it.

### Controls only a PC can carry out no longer pretend otherwise

Rescan, Open folder and Create folders all act on the games folder **on the PC**. With the PS5
serving the page the console answered every one `{"ok":false,"error":"not supported on console"}` and
not one caller looked:

| Control | What it said | What had happened |
|---|---|---|
| Rescan | "Library rescanned — **undefined** titles" | nothing |
| Rescan (toolbar) | "Library: rescanned" | nothing |
| Open folder | a failure, drawn in the **success** colour | nothing |
| Verify | "Inconclusive — **undefined**" | nothing |

They are disabled together now, with a tooltip saying which machine can do it, and they re-enable by
themselves the moment a PC answers — `resolveApi()` already clears the flag, so no reload is needed.
Every one of those handlers reads `ok:false` properly now too, because a control can be enabled and
*still* get an error back.

### Move to another drive was dead on the PC, and offered drives that were not there

`/api/move` and `/api/move/status` existed only on the console, so with the PC serving the page the
move posted to a route the companion had never had. Both are forwarded now.

The picker was worse than dead: `PS5_DEVICES` is a hardcoded list of eleven ids with no free space
and no check that any are plugged in, so it offered **USB1–USB7 on a console that has none of them**,
each showing an undefined size — and the space check `dst.free && dst.free < g.size` passed silently
because `undefined` is falsy, so the move started and failed on the console instead of being refused
here. The companion asks the console now (real `statvfs` figures and a `detected` flag) and falls
back to the static list only when there is no console to ask. Measured: three real drives, not eleven.

### The queue's ✕ and Retry did nothing on the console

The console implemented `/cancel` but not `/dismiss` or `/retry`, and both callers ignore the
error — so the row sat there and the arrow did nothing. That is the *"it doesn't even let me remove
them from the queue"* complaint, reproduced exactly whenever no PC is running. Both verbs land on the
console's single install slot now: dismiss clears a slot that is not running, retry re-runs the
package the slot still remembers — which is the point of retry when there is no PC to re-send from.

### The last of the etaHEN / Elf Arsenal residue

The install lane, client and config went in 3.36–3.38. What survived was the **reporting**:
`/api/health` still announced `dpi_online` / `dpi_reachable` / `dpi_state` / `dpi_port:12800`, and the
console recomputed them **on every poll with a live TCP probe of :12800** — a port nothing of ours has
bound since 3.36.0. Every health request, several times a minute, paid for a connect to a port that
exists only if the user is running someone else's daemon. All four fields are gone, and the probe
with them.

Also gone: the header's *" · connected (DPI off)"* branch; a **Send to PS5** tooltip still selling
*"the Debug Package Installer (no USB, no DPI)"*; `_host_kind` / `_host_kind_at`, cached state for
"which foreign daemon owns :12800", assigned once and never read; and the comment block above
`DEFAULT_CONFIG["dpi"]` still documenting `mode`, `host` and `auto_reload` as though they could be
set, three lines above the note saying they were removed.

### Smaller things

- **Seven tooltips the PS5 browser never showed.** `title=` renders on hover; the console is driven
  with a controller. All seven moved to the project's own `data-tip`, which draws on focus too — the
  settings button had no accessible name at all beyond that invisible `title`.
- **`capabilities.uninstall` was dead** — hardcoded `false` by the companion, never sent by the
  console, so the clean-reinstall branch guarded by it had never run once. Clean reinstall splits by
  what the title *is* now: a backup is deleted and re-sent in one step; a package keeps the dashboard
  instruction, because that genuinely does remove it completely.
- **The version stamper warned instead of stamping.** It printed *"companion is 3.40.0 but the ELF is
  3.39.0"* and carried on — and a warning in the middle of a long build scrolls past. It stamps
  `server.c` now. Today that mismatch meant the only way to tell which build was on the console was
  the compile timestamp in `/api/health`.
- The console's "why is the percentage unknown" comment still blamed etaHEN for doing the fetching.
  The real reason is that our installer runs as a separate spawned process — the separation that
  makes installs work at all — and it reports a verdict rather than progress.

---

## [3.33.0] — 2026-08-25 · "The PS5 lane finally runs on our own engine" `[VERIFIED]`

### The queue trapped two jobs, and it was my guard that did it

The duplicate-submission guard added hours earlier asked **"did we send this URL in the last 30
minutes?"** That is not the dangerous condition. The dangerous condition is *the console is already
fetching or installing this content*, which is what actually preceded the crash.

The difference showed up the moment the user tried anything real: they deleted two games, reinstalled
four minutes later, and both were refused with *"may still be installing it"* — false; the installs
had finished — and told to *use Force*, which the UI has no button for. Two jobs held, unstartable.

It asks the console now. `bgft.db` says whether a title has a live job (status `1000`/`1009`),
`install_job_row()` already reads it, and `Queue._run` already fetches that row one line earlier for
another purpose — so the correct check costs nothing and cannot produce that false positive. What
remains in `install_spawn` is a 120-second debounce that clears on the verdict (~2 s later), so it
only ever catches a genuine double-click.

**Verified:** both games queued and installed with no force — 3.7 s and 7.3 s, `bgft 1036`, five
files each, and the bgft `title` column reading their real names.

### Reinstall is now offered instead of refused

A title that genuinely is installed used to be a dead end: a toast, a greyed button, and no way to
say *"yes, put it back anyway"* without editing config. `force` had always been plumbed from
`installItem()` through `POST /api/install`; nothing in the UI ever passed it.

> **Riptide GP2 is already installed on PS5.**
> Reinstall it anyway? The console overwrites the existing copy. Your saves are not touched.

Confirmed → force → reinstalled in 8.5 s. The server-side message dropped *"delete it there first
if you want to replace it"*, which stopped being true the moment the prompt existed, and now names
the game rather than its title id — a question someone can answer.

### X on a queue row now removes it

The backend cancel worked the whole time; the row simply **stayed**, reading "Canceled". The only
control that removes rows is a small icon in the dock header that nobody finds. New
`POST /api/queue/<id>/dismiss`: a finished row goes immediately, a live one is stopped first and
then goes. `dismiss()` refuses while a task is still running, because a task that vanishes from
under its own worker is how a queue starts reporting states for jobs nobody can see.

---

## The PS5 backup lane, reconnected to our engine

A `.ffpfsc` backup is never installed — ShadowMount mounts it. All we have to do is put the
container in the chosen drive's homebrew folder. That worked, but the **PC pushed** the file over
the file API: built for the Elf Arsenal era, never reconnected to our own downloader.

**`GET /api/engine/fetch?url=&dest=&name=&size=`** — download-only, on the console:

- our own `http_download()` — resumable, Range-aware, the same code that moves PS4 packages;
- writes `<name>.part` and renames only once the last byte lands, because ShadowMount mounts
  whatever appears in that folder and a half-written container mounts as a broken game;
- **preserves the filename exactly.** `/api/engine/install-url` sanitises non-alphanumerics into
  underscores, which would turn `[PS5] PPSA27616 - Bendy and the Ink Machine.ffpfsc` into something
  ShadowMount would not recognise. That is why this is a sibling endpoint and not a flag.

### Delegation was the wrong shape once the console can pull

A backup on another PC used to be handled by asking **that** PC to run its own mount lane and
mirroring its progress — one hop instead of two. Sound reasoning, real cost: the other machine has
to run our code, report progress, and stay up for the whole transfer, and the result is only as good
as whatever build it happens to have.

With the console pulling, the same single hop happens and the other PC only has to **serve the
file** — a static HTTP read it was already doing. Peer backups are queued as `mount` tasks carrying
both routes; delegation survives as the fallback for a console whose ELF predates the endpoint, and
the choice is made when the task *runs*, not when it is queued.

**Verified end to end, both titles previously deleted, pulled from a different PC (Casita):**

| | Bendy and the Ink Machine | Evergate |
|---|---|---|
| size | 1 346 961 408 | 1 983 840 256 |
| pulled in | ~13 s (≈100 MB/s) | ~19 s |
| mounted + confirmed | **31.8 s total** | **50.0 s total** |
| filename on disk | exact, spaces and brackets intact | exact |
| `.part` left behind | none | none |

```
fetch: requested  http://10.0.0.72:8710/library/%5BPS5%5D%20PPSA27616%20-%20Bendy…ffpfsc -> /mnt/ext1/homebrew
fetch: delivered  /mnt/ext1/homebrew/[PS5] PPSA27616 - Bendy and the Ink Machine.ffpfsc
```

Nothing in this lane touches etaHEN, Elf Arsenal, DPI or `:12800`.

### "Internal" is a 31 GB system partition

Read off the console: `internal` resolves to `/data/homebrew`, which held **zero** backups — the
real ones are on `ext1` (32) and `usb0` (18). Internal has **31 GB free of 673 GB**, and PS5 backups
in this library run to 125 GB. Sending one there would fill the system partition, and finding out
after an hour of transfer is not an answer.

`/api/engine/fetch` now checks `statvfs` against the declared size before accepting, with 256 MB of
headroom:

```
{"ok":false,"error":"Not enough room on that drive - BigGame.ffpfsc needs 512000 MB
 and /data/homebrew has 29393 MB free. Pick another drive."}
```

It also write-tests the destination first, so an absent or read-only drive says so in a second.

---

## etaHEN is no longer shipped at all

We stopped *starting* it long ago, but the ELF still embedded 4.7 MB of it and wrote it to
`/data/pkg-mutant-shop/payloads/` **on every boot** — for a daemon we never run, that the user does
not want, and whose presence on disk was the only thing making an accidental launch possible.
Nothing in our lane read it: `pb_etahen` appeared only in the bundle table, and the companion's
`host_payload_path()` resolves through Payload Manager's own directory, in the legacy `v2` mode we
return early from anyway.

Removed from the bundle, and the app now sweeps what it already left:

```
boot: removed etaHEN.elf - no longer shipped
boot: removed PKG-MUTANT-SHOP.elf - no longer shipped
```

**The ELF is 37.3 MB, down from 42.1 MB.** Only `shadowmountplus.elf` and `pms-installer.elf`
remain in our payload folder.

### What `pms-installer.elf` is, since it appears in Payload Manager

It is our install engine — the separate process that exists because
`sceAppInstUtilInstallByPackage` returns `0x80B2116F` from an injected payload and `0` from a
spawned one. It runs for about two seconds per install and exits; Payload Manager **lists** it as an
available payload, which is why it looks active. Confirmed against the live process list: the only
thing of ours running is `payload.elf` (the shop). It sits in pldmgr's directory because
`/loadpayload` resolves by basename against that directory.

---

## [3.32.0] — 2026-08-25 · "The message that crashed the console"

### ✅ THE INDEPENDENCE TEST — PASSED on a console that never loaded etaHEN `[VERIFIED]`

Fresh Y2JB. The user's stack only: Payload Manager, nanoDNS, ftpsrv, kstuff-lite, ShadowMount.
**etaHEN, ps5debug NG and everything else were never started this boot.** Baseline before touching
anything: 12800 / 9090 / 9081 / 1337 / 9040 all closed, 8710 closed.

Deployed over FTP to `.part` + verified byte count + rename, loaded once via Payload Manager, and
**up in 1.6 seconds**:

```
version 3.32.0   built "Aug 25 2026 15:43:24"   engine pms-spawn
engine_ready true   dpi_online false   shadowmount true (10101)   ftp 2121
```

**Test A — console-direct spawn, no display name.** Riptide GP2, not installed, nothing forced:

```
rc 0x00000000   via spawned-process   pid 182   authid 0x4801000000000013
content_id EP0786-CUSA02365_00-RIPTIDEGP2PS4001
```

**Test B — the full PC lane, never verified until now.** Castle Crashers Remastered, not installed,
nothing forced. Queue → console → confirmed in **6.1 seconds**:

```
0.0s queued  1.2s submitting  3.6s transferring 53%  4.9s promoting 90%  6.1s playable
```

**The proof, from the console's own records:**

| | Riptide GP2 `CUSA02365` | Castle Crashers `CUSA14409` |
|---|---|---|
| bgft status | **1036** | **1036** |
| bytes | 107 806 720 / 107 806 720 | 227 540 992 / 227 540 992 |
| bgft `title` column | `PKG MUTANT SHOP` | **`Castle Crashers Remastered`** |
| files at `/mnt/ext1/user/app/<TID>/` | 5 | 5 |
| `/user/appmeta/<TID>` | 9 entries | 8 entries |
| `tbl_contentinfo` | registered, 108 527 616 | registered, 228 261 888 |

`app.db integrity_check = ok`. **All five third-party install ports still closed afterwards.**

Three of this release's fixes are visible working in the console's own log:

```
install: verdict {"ok":true,"rc":"0x00000000",...}          <- Test A
install: previous verdict was never collected - releasing the lane
install: requested (async) uri=...Castle-Crashers...        <- Test B started immediately
install: verdict {"ok":true,"rc":"0x00000000",...}
install: cleaned up - ready for the next one
```

The middle line is the busy-latch auto-release: Test A's verdict was never collected, and the lane
freed itself instead of blocking Test B for the full 600-second expiry. And `title` reading
**`Castle Crashers Remastered`** rather than `PKG MUTANT SHOP` is the name passthrough landing —
before it, every PC-driven install filed itself under the app's own name.

### The crash, closed out

`install.log` survived (append-only, `O_SYNC`) and was recovered over FTP before anything was
loaded. It corroborates the audit independently:

```
1787680030  install: verdict rc=0x00000000      <- first install COMPLETED
1787680368  ==== BOOT: PKG MUTANT SHOP 3.31.0 ==== <- the reload, on bad advice
1787680392  install: requested (async)          <- the duplicate, 24s later
1787680392  install: installer spawned rc=0
            <no verdict — the console died inside InstallByPackage>
```

And `bgft.db` names the victim: `CUSA02365 status 1022, 65536/107806720, title="PKG"` sitting on top
of a completed `1036`. So it was a re-install over a **fully registered** title — reached only
because the test passed `force: true`, overriding the already-installed guard this project had
deliberately built. Both halves are now defended: the guard is honoured, and a repeat of the same
URL inside 30 minutes is refused on the PC side, where a console reload cannot erase it.


A 7-lens audit (81 agents, every non-cosmetic finding attacked by an independent skeptic, 73
findings, 34 survived) went looking for what killed the console. It found the chain by reading
`pms.log`, and the first link was a sentence added earlier the same day.

### What actually happened

```
13:49        install-spawn accepted  ->  rc=0, content_id   (a LIVE BGFT download now exists)
13:50:25     the queue sends the same package
             the console answers  409 Conflict  "an install is already being handed to the console"
             the companion reports:  "PKG MUTANT SHOP on the PS5 did not answer -
                                      load it again from Payload Manager"
~13:51       the ELF is reloaded on that advice
             a fresh process starts with  static volatile int g_spawn_busy = 0
             >>> THE RELOAD DELETED THE REFUSAL <<<
~13:53       the same package is submitted again, on top of the still-running download
             installer spawned, no verdict, console gone - including pldmgr and elfldr,
             which are not ours
```

`_shop()` uses `urlopen`, which **raises `HTTPError` on 409**, and the caller caught it with a bare
`except Exception` that asserted a cause it had never checked. Before that message was "improved" it
printed `repr(e)` — `<HTTPError 409: 'Conflict'>` — and nobody would have reloaded anything.

**An error message that names a cause it has not verified is worse than a raw exception.**

### The four fixes that break that chain

- **`ShopHTTPError`** carries the status and the console's own sentence. A 409 now relays *"An
  install is already running on the console — let it finish"* and sets `do_not_reload: True`. The
  queue holds the job as `console_busy` instead of erroring.
- **A missing verdict is "state unknown", not "nothing was queued."** The 90-second timeout returned
  `queued: False`, which `Queue._run` treats as *provably safe to re-drive* — a gate written for a
  daemon that validates before queueing. Ours does not. It now returns `no_verdict: True` and the
  job is **held**. At 13:54:43, with the console already dead, the old code logged "retrying once".
- **The duplicate guard lives on the PC**, in `Bridge._accepted`, because the console's latch is
  cleared by any reload — which is precisely the failure mode. Overridable with `force`, which is
  now plumbed from `POST /api/install` all the way down.
- **The poll loop tells "slow" from "dead."** `except Exception: continue` treated the expected 404
  ("no verdict yet") and a dead console identically and logged neither, which is why there is a
  **124-second hole in the record with the fatal window inside it**. Three consecutive transport
  failures now stop the loop and say so.

### Ruled out by proof — do not re-investigate

- **The display-name passthrough.** `fixed[1300]` + `nm[300]` → `line[1750]`: worst case 1600 bytes,
  150 of margin, `snprintf` cannot truncate so the `write()` length is always real. Bounded again to
  239 bytes in the installer. The actual payload was `Riptide GP2` — 11 ASCII characters. The prime
  suspect was wrong.
- **The busy-latch auto-release race.** Real bug, but it needs `localinst_thread` running
  concurrently; the fatal attempt was companion-driven only.
- **The double `url_decode`.** `Riptide%20GP2` → `Riptide GP2` → `Riptide GP2`. A no-op here.

### Console hardening, defensive only — the call that installs is untouched

`spawn_installer_write()` streamed 117,952 bytes straight over the file Payload Manager is about to
**execute**, ignoring a failed `open()`, abandoning the file on a short write, with no `fsync`, no
size check, and a `void` return nobody could check. `/data` also holds the logs, the cheat library
and the web bundle, so ENOSPC there is not hypothetical — and a truncated ELF then goes to elfldr.

It now writes to `.part`, verifies the byte count, fsyncs, renames, returns `int`, and **both** lanes
refuse to spawn if it failed. The same 118 KB was also being written a **second time inline**, a
verbatim duplicate — four truncate-and-rewrite cycles of the live payload per request, the last two
immediately before execution. Deleted.

Two failure paths also left `g_spawn_busy` set, locking the queue out for 600 seconds;
`spawn_install_wait` had always cleared it and the async lane never did.

### Every build is now distinguishable

`SHOP_VERSION` is hand-edited, and on 2026-08-25 **two different ELFs both reported `3.31.0`** — so
nothing could say which binary was answering when the console died. `/api/health` now reports
`built` from `__DATE__ " " __TIME__`, which the compiler fills in whether or not anyone remembers to
bump the version.

### The exe had no UI gate at all

`check_web.py` and `stamp_version.py` ran only in the ELF build. Building just the exe could package
a UI whose script does not parse — which blanks the app on every device while the server keeps
answering 200 — or a stale `APP_VERSION`. It stayed safe only because the ELF happened to be built
first every time; ordering is not a safeguard. Both now run inside `PKG-MUTANT-SHOP.spec` with the
UI gate **fatal**, and `build_exe.cmd` builds from the spec instead of its own drifted flags.

### `MUTANT PKG ENGINE.md` was describing a lane we no longer have

It opens with *"If the engine is broken, work from this document"* and then hands every install to
etaHEN on `:12800`. §2.1 stated flatly: **"`:12800` — the install host — is started by us."**
Following it would put back the daemon this project spent two weeks removing.

Rewritten from source: a new **§0** (what the engine is today), §1, §2.1, §2.2, §2.3, §4, §12, §14.2,
plus §13.6 for the `-O2` landmine — and a header saying exactly which sections are current and which
are etaHEN-era history kept for diagnosis. Three of its claims were wrong in ways that mattered:

- §2.2 still called `0x80B2116F` unsolved, "12 hypotheses eliminated".
- §2.3 equated `dpi.mode="pms"` with `AppInstallPkg`, the call that "cost a console, twice".
  Verified from source: `install_full()` calls **only** `g_ai_installbypkg`; there is no
  `AppInstallPkg` fallback for a game anywhere, and `AppInstallPkg` has exactly two call sites —
  `tile_install()` and the diagnostics-gated `/api/engine/install-local`.
- §4 put ShadowMount on 9021 (it is 10101; 9021 is elfldr, always up).

The Settings engine picker is relabelled to match: `pms` is *"Ours - in-process (fails on base
games)"*, not the neutral phrasing it shipped with.

### New: `tools/verify_console.py`

Ten stages in an order that **separates the hypotheses** rather than just confirming things work —
install with no name, then a plain ASCII name, then a non-ASCII name, then the full PC-driven lane —
with a console health probe between every stage, so if it dies the stage number names the culprit.
`/api/open` is deliberately last. Stage 0 asserts 12800/9090/9081/1337/9040 are **closed** (the
independence proof); stage 9 pulls `bgft.db` through our own file API and checks the row says
`PKG MUTANT SHOP`. `--forensics` reads the console's install log and changes nothing.

---

## [3.31.0] — 2026-08-25 · "Every word, and a Settings page that describes the app we actually built"

### `-O2` BREAKS THE INSTALLER. Build it with `-g`. `[VERIFIED — both directions]`

The single most important line in this release. `pms-installer.elf` had always been built by hand;
building it this time with `-O2` instead of `-g` produced a binary that fails **every** install:

```
  -O2  sceAppInstUtilInstallByPackage -> 0x80B2116F, content_id empty
  -g   the identical source, same console, same URI, minutes apart -> 0x00000000, content_id set
```

Same process type (`via: spawned-process`), same `authid` (`0x4801000000000013`), same everything
the last two weeks of work established as load-bearing. Only the optimiser changed. The likely
mechanism is the 9984-byte `PlayGoInfo` the call takes by pointer: only a few of its fields are
written, and at `-O2` the compiler is free to treat the rest of that stack object as dead.

Two things now stop this happening again:

- **`ps5-app/onconsole/build-installer-wsl.sh`** exists, pins `-Wall -g`, and says why in a comment.
- **`build-wsl.sh` calls it first.** `payload_bundle.h` `.incbin`s the installer straight into the
  shop ELF, so building the shop without rebuilding the installer shipped a new shop carrying an
  old installer — silently. That is exactly how it drifted.

### Every companion-driven install said "Your game"

`/api/engine/install-spawn?uri=…&name=…` read `uri` and dropped `name`, so `installer-req.txt` had
no second line and the spawned installer fell back to its placeholder. The blocking lane (USB
installs, started on the console) passed the name through, which is why every test done from the
console itself looked right. Every install started from a PC — that is nearly all of them —
announced itself as *"Your game is installing"*.

### The busy latch could outlive the install it was guarding

Reproduced: an install finished, the installer wrote its verdict and exited, and the latch stayed
set because only `spawn-cleanup` clears it and the caller had gone away. The next install then
waited behind a flag protecting a process that no longer existed — measured at **122 seconds of an
apparently frozen queue row**, followed by *"PKG MUTANT SHOP on the PS5 did not answer"*, which was
untrue: the console had answered every poll.

- **The console releases it itself.** A verdict file on disk is proof the job is over, so
  `install-spawn` and `spawn-status` both treat "has result" as "not busy".
- **The companion waits 30 s, not 120 s**, then clears the latch and carries on, and says so in the
  log rather than inventing a cause.

### The queue reported the wrong reason for every refusal

```python
if not bridge.ping():          # "is a daemon listening on :12800"
    ... "The console stopped answering"
```

`ping()` is permanently false in our own lane — nothing binds 12800 any more — so **every** package
the console refused was reported as the console having vanished, sending the user off to reload a
payload that was fine. It asks `engine_available()` now when our engine is in play, and a refusal
gets a branch of its own that repeats what the console actually said.

---

## Every word the shop says

212 messages, read out of the source by **`tools/message_report.py`**, which also enforces the
house style with `--check`:

> line 1 is the event, naming its subject · line 2 is what it means or what to do · the words
> "PKG MUTANT SHOP" appear only when the shop itself is the subject, because the icon already says
> who is speaking · no raw error codes, JSON or file paths on a television · no bare-verb failures ·
> no reference to software we do not ship

`python tools/message_report.py --check` → **212 checked, 0 off style.**

### One error table, three binaries

`0x80B2116F` used to reach the television as `0x80B2116F`. A number is not an answer.
`install_error_text()` now exists in the shop ELF, the installer ELF and the companion — three
copies because they are three separate binaries, each annotated to say so — and decodes the five
families this project has actually observed on hardware, with fallbacks that keep an unseen code
producing a true, actionable sentence.

### A parsing bug that pasted a JSON document onto the TV

```c
const char *rcp = strstr(buf, "\"rc\":\"");
snprintf(reply, rsz, "the console refused this package (%s)", rcp ? rcp + 6 : "…");
```

`rcp + 6` points at the digits, but there is no terminator there — it is the middle of a JSON
document. So the "reason" shown on the television and in the queue was:

```
the console refused this package (0x80B2116F","init_rc":"0x00000000","via":"spawned-process",
"pid":447,"authid":"0x4801000000000013","content_id":"","uri":"http://10.0.0.76:8710/…)
```

### Duplicates removed

- `notify_branded()` / `notify_branded_f()` were byte-for-byte copies of `notify()` / `notifyf()`.
  Two names for one behaviour is how the cheat toasts drifted into four wordings; they are gone.
- **Four** copies of the cheat-result wording existed. There are two functions now —
  `cheat_result_toast()` and `patch_result_toast()` — and the patch route no longer prints its
  engineer-facing `detail` on a television, nor rounds a partial apply up to "applied".
- The cheat-intake toast was written out three times in three files. `notify_cheats_filed()`.

### `errText()` — one place in the UI

Thirty-odd toasts each did their own `((r&&r.error)||"?")`, so what you saw depended on which
button you pressed: *"Move failed: ?"*, *"Could not toggle: no_local_cheat_found"*, *"Could not
stop them: unknown"*. `errText()` prefers a sentence the server sent, falls back to a known
translation, and — for a code nobody has taught it yet — prettifies the identifier rather than
showing `?`. A new server-side code degrades to readable English on its own.

---

## Settings, rebuilt around the app we actually have

**The row this panel got most wrong** read `Install engine  10.0.0.99:12800` with an LED that meant
"is a third-party daemon listening". With our own engine running perfectly it showed a red light
next to a port nothing binds. It is replaced by what our lane actually needs, each with its own
light: **the shop answering on the console** and **Payload Manager answering on :8084** — plus a
sentence underneath that says what to do when one of them is missing, from the new
`GET /api/engine/state`.

**The port box was `dpi.port_v2`.** Typing in it changed nothing and saving it wrote a dead
setting. It is `console.shop_port` now — the port everything actually uses.

**Four settings existed only in `config.json`.** How many installs run at once, whether to check a
package before handing it over, whether to watch the games folder, and *which engine installs go
through* — all editable by hand next to the exe, which means nobody ever edited them. They are on
the page, and Save writes them. Changing one shows **"Not saved yet"** until you do.

**"Open on PS5" has never worked.** It opened a socket to `console.notify_port` (9099); nothing has
ever listened there. The console has `GET /api/open` now, which resolves
`sceSystemServiceLaunchWebBrowser` by name — **available on 12.70, returns 0, the browser opens** —
and always sends a notification as well, so the control does something visible either way.
`Bridge.notify()` had the same dead 9099 socket and has the same fix.

**The install log was unreachable.** The console has kept `/data/pkg-mutant-shop/install.log` since
3.29.0 and nothing in the app could read it. There is a button, and `apiText()` copes with the
console answering text and the companion answering JSON.

Also: `config_unreadable` is surfaced (the app silently ran on defaults and refused to save);
`paintServices()` replaces two service blocks that had already drifted apart; the PS5 itself joined
the "devices on this network" list; `state.online` means "reachable" instead of "is a daemon on
12800"; and the library path fallback printed **`C:Mutant Games`** — `\M` is not an escape in a
JavaScript string, so the backslash was silently dropped.

---

### Verified on hardware at 3.31.0

- ELF built, uploaded through our own file API (**42 MB in 2.5 s**) and reloaded via Payload
  Manager; console came back on 3.31.0 with `engine_ready: true`
- `GET /api/open` → `launched: true, rc: 0` — the PS5 browser opened
- `GET /api/engine/state`, `/api/engine/log`, `/api/engine/spawn-status` (now with `busy_for`,
  `stale`, `log_bytes`)
- `/api/cheats/library` returns real counts — the route-order collision with
  `path.startswith("/api/cheats/")` is fixed
- `/api/dpi/reload` clears spawn state instead of relaunching a third-party payload
- **a full spawned install: `rc=0x00000000`, `content_id=EP0786-CUSA02365_00-RIPTIDEGP2PS4001`**
- Settings panel checked in a live browser: every control bound, every LED honest with the console
  off, `C:\Mutant Games` correct, the config warning correctly hidden

### Not verified

The companion-driven end-to-end run after the latch and name fixes did not complete: the console
dropped off the network mid-install — **all** ports, including Payload Manager and elfldr, which
are not ours, and it stopped answering ICMP. That is the console powering down or resting, not our
payload. It needs re-running once the console is back.

---

## [3.30.0] — 2026-08-25 · "Messages that were confidently wrong" `[VERIFIED]`

A full inventory of every user-visible string in the app — **603 of them** across the television,
the interface, the button captions and the server's own replies — turned up several that were not
merely awkward but **stating things that were not true**. Reference published at
`research/message-reference.html`.

### "Nothing was written." — while patches were live in the game

`cheat_apply_mod` ends with `return failed ? -(100 + failed) : written;`. So `rc <= -100` means
*some entries were refused* — it does **not** mean nothing landed. A mod whose first two writes
succeeded and whose third was refused returns `-101`, and the toast said:

> *"X not applied — The game code does not match this cheat file. Nothing was written."*

while two patches were live in the running game. That is the worst shape of wrong: it tells someone
their game is untouched when it is half-patched. It now parses `written=` back out of the detail and
splits:

> *"X is only partly ON — 2 patches applied before the rest were refused. This cheat looks built for
> another version of the game. Turn it off to undo them."*

### "Move failed / God of War"

The second line of that toast printed the **game name** where a reason belongs — the shape of an
explanation with none in it. The actual cause ("the destination drive may be full") was written to
`g_move.error` and only ever reached `/api/move/status`. All three move failures now name the game,
give the cause, and say the original was not removed.

### A clean sweep that wasn't

*"All cheats turned off — 3 mods reverted"* counted cheats that were **already off** as reverted,
and computed a `failed` count it then never mentioned. Three undone and two refused still announced
success. Now three distinct outcomes, and failures are spoken.

### Two copies of the same messages, already drifting

The two cheat routes each had their own copy: one said *"The game code"*, the other *"The game's
code"*; one separated with `" - "`, the other with `" · "`. A fourth branch existed on one route and
not the other, so an identical failure was **spoken on one lane and silent on the other**. All of it
now goes through one `cheat_result_toast()`.

Also: `"%s could not be applied
%s"` piped an engineer-facing string (`cannot read <truncated
path>`, `mod 4 not found`) onto the television. The detail still reaches the API response and the
install log — it just no longer goes on screen.

### [audit 29] A twenty-second stopwatch, and a title list mistaken for proof

The in-process engine waited 20 × `sleep(1)` for a title to appear, then declared `JOB_DONE
"Installed via X"` or `JOB_ERROR "never appeared"`. Both halves were wrong: twenty seconds is not
long enough for a large title on a busy console, and **appearing in the title list is not proof the
game is there** — a metadata-only registration does exactly that, and that is the tile that fails
with "Cannot start the game". It now waits on evidence (up to 300 s), and requires the game's own
`app.pkg` on disk before saying installed. A title that registers with no data behind it gets its
own message telling the user *not to launch it*.

### [audit 05] Single-instance guard

Two companions on one port is not hypothetical — the loser of the bind race keeps every background
thread running (library rescan, console probes, `installed.json` writes), which is how one process
overwrites the other's record of what is installed.

The first attempt at this guard **was itself wrong**: it asked `/api/health` with a 2-second timeout,
so it timed out on exactly the slowness that makes the guard matter and would have started a second
copy anyway. A guard that fails open under the conditions it exists for is worse than none. It is a
TCP connect now, with the version lookup as decoration. Verified by launching a second copy: it
refused and logged
`[boot] PKG MUTANT SHOP v3.30.0 is ALREADY running on port 8710 - not starting a second copy.`

### Found by probing our own endpoints

- **The ⟳ reload control was resurrecting etaHEN.** `recover_dpi()` relaunches
  `host_payload_path()` — the etaHEN copy we ship ourselves — so pressing it after removing etaHEN
  silently brought it back. Caught because etaHEN reappeared with a fresh pid minutes after being
  killed, and the only thing in between was that probe. In our own lanes it now clears the install
  state and says there is nothing to reload: **0.05 s instead of 4 s**, and etaHEN stays gone.
- **`/api/health` took 3.3 s** — the most-polled endpoint in the app. The `engine_ready` check added
  in 3.28.1 put a console round trip on every call. Cached for 5 s: **3.3 s → 0.05 s**.
- **`/api/cheats/paths` still advertised `ftp_port: 2121`.** It now names our own
  `/api/fs/write` and only mentions FTP when one is actually running.

---

## [3.29.0] — 2026-08-25 · "The 30-second mystery toast, and every message says something true" `[VERIFIED]`

### The toast that appeared out of nowhere

`"PKG MUTANT SHOP is ready · Mounts on · Installs on · FTP on"` fired roughly **30 seconds** after
the app started — `AUTOSTART_DELAY_DEFAULT` — with no visible cause, long after the version toast
had already said we were up.

It was also, by then, **lying in the most confusing direction possible**. "Installs" meant *is a
third-party daemon listening on `:12800`*, so with etaHEN removed it announced **"Installs off"**
while installs were working perfectly through our own engine, and **"FTP off"** while file access
was fine through ours.

Both halves are fixed:

- It now speaks **only when it actually started something** (`started_any`). If everything was
  already running — the normal case — it says nothing at all, because the startup toast already did.
- When it does speak it reports what it changed, not a port belonging to software we do not use.

### One startup toast, and it tells you what to do

```
  before:  PKG MUTANT SHOP v3.28.2
  after:   PKG MUTANT SHOP v3.29.0 is ready
           Open it from Media, or 10.0.0.99:8710 in any browser
```

It fires the moment the server is listening — which is also the moment installs become possible,
because our engine is spawned per install and needs nothing running beforehand. "The shop is up" and
"you can install" are now the same event, so one toast covers both.

### Every toast carries our icon

The artwork form existed since the notification code was written, but only as an opt-in test
(`?icon=1`) that was never made the default. Measured on this console:
`sceKernelSendNotificationRequest` returns `0` **with and without** the icon, so there was no reason
left to send unbranded toasts. `notify()`, `notifyf()` and `notify_sync()` all brand now.

**It is the ICON, not the LOGO.** `web/assets/icon0.png` is the square 512×512 mark;
`web/assets/logo.png` is the wide wordmark and would be cropped to nothing in a square notification
slot. `notify_icon()` falls back to a plain toast when the file is not on disk yet, so a console
still extracting `web/` on first boot gets a plain notification rather than a broken one.

### Messages that had gone stale

- *"Installing X — Reading it from the drive, this takes a few minutes"* described the old lane. A
  USB install is a hand-off now: **"Handing X to the console — it installs from the drive itself"**.
- *"Network busy — could not start"* said nothing actionable. Now: **"Another copy may already be
  running — check Payload Manager"**, which is what it almost always is.
- *"Open the tile, or the browser at 127.0.0.1:8710"* → **"No need to load it again — open it from
  Media"**.

### The console keeps its own install log

The PC got `pms.log` in 3.25.3 and it paid for itself immediately. The console had nothing: with the
PC switched off, or when the spawned installer was involved, there was no record at all — only
whatever toast happened to be on screen for four seconds.

`/data/pkg-mutant-shop/install.log`, readable at **`GET /api/engine/log`**. Same rules as the power
watchdog's log and for the same reasons: append-only, `O_SYNC` so a crash or a power cut cannot
swallow the last lines, trimmed only at startup (never mid-install), bounded at 512 KB.

Live, two queued installs:

```
1787672997.847  install: requested (async)  uri=http://10.0.0.76:8710/library/Riptide-GP2-CUSA02365.pkg
1787672997.899  install: installer spawned  rc=0
1787672999.905  install: verdict  {"ok":true,"rc":"0x00000000","content_id":"EP0786-CUSA02365_00-…"}
1787672999.952  install: cleaned up - ready for the next one
1787673006.139  install: requested (async)  uri=…Castle-Crashers-Remastered-CUSA14409.pkg
1787673006.198  install: installer spawned  rc=0
1787673008.239  install: verdict  {"ok":true,"rc":"0x00000000","content_id":"UP2015-CUSA14409_00-…"}
1787673008.267  install: cleaned up - ready for the next one
```

Complete cycles, both lanes covered — the async endpoint the companion drives *and* the blocking
one the USB lane uses. The verdict is logged once per job rather than on every poll.

---

## [3.28.0] — 2026-08-25 · "A game installed with etaHEN entirely dead" `[VERIFIED]`

### The test

`etaHEN Critical ser` (pid 370) killed first — it restarts the Utility daemon, so that order
matters — then `etaHEN Utility Daem` (438). Every etaHEN port closed:

```
:12800 closed   :9090 closed   :1337 closed   :9081 closed   :9028 closed
:8084  OPEN (Payload Manager)  :8710 OPEN (ours)
```

Then Riptide GP2 installed through the app, 8.7 s, `submitting → promoting → "Ready to play"`:

```
bgft #237  status=1036  title='PKG MUTANT SHOP'  107806720/107806720  err=0x00000000
/mnt/ext1/user/app/CUSA02365/  app.pkg 107806720 + app.pbm + app.pbm.backup + app.json + app.xml
```

That `bgft.db` was read through **our own `/api/fs/read`**, because etaHEN's FTP was dead too.

### Why it works — Arsenal and etaHEN were only ever gateways

Neither provides fake-signed package support. **kstuff** does, and Payload Manager autoloads it
independently:

```
/data/pldmgr/autoload.txt:  !5000 / nanodns.elf / ftpsrv.elf / kstuff-lite_v1.10.elf / shadowmountplus.elf
```

etaHEN is not in that list. Three independent confirmations, from the **running 2.6B binary**, not
just the 2.5B source:

- **Byepervisor (etaHEN's own fake-sign machinery) never runs on 12.70.** At va `0x13c40`:
  `cmp DWORD PTR [rbp-0x14],0x3000000` / `jae` — it jumps clean over the Byepervisor call. This
  console reports `12700001`.
- **etaHEN does not even load kstuff here.** 2.6B skips its own copy when
  `sceKernelMprotect(buf,100,PROT_RWX)==0` — i.e. when kstuff is already live. The running
  processes are `kstuff.elf` (pids 95, 265), *not* `kstuff`, which is the name etaHEN's
  `elfldr_spawn` would give it.
- **`patchShellCore()` and `pause_resume_kstuff()` are no-ops on 12.70** — their firmware tables
  stop at 8.20 and 10.60. The console's own `/data/etaHEN/etaHEN.log` line 8 reads
  `Unsupported firmware`.

Corroborated by the timeline: etaHEN first reached this console on **2026-08-19**, but fake-signed
games installed here on **2026-08-03** through Elf Arsenal.

### What still pointed at etaHEN in OUR code, now gone

- **The console-local (USB) lane** required `port_open(12800)` and POSTed to whatever owned it. It
  now calls `spawn_install_wait()` — our engine — so a package on a USB stick installs with the PC
  off and no third-party daemon anywhere.
- **"Install ready" meant "something is listening on :12800".** `/api/health` now reports
  `engine: "pms-spawn"` and `engine_ready`, which is Payload Manager being reachable — the only
  thing our engine actually needs. `dpi_*` is kept so an older UI does not break.

### One condition NOT yet tested — stated plainly

etaHEN's **bootstrapper** makes boot-time changes that outlive its processes: `/system_ex` and
`/system` remounted read-write via nullfs `MNT_UPDATE`, `/update/PS5UPDATE.PUP` unlinked and
`/update` force-unmounted (`bootstrapper/source/main.cpp:1140-1160`). Nothing reverts them, so they
persisted through this test — etaHEN had already booted before it was killed.

A console that **never runs etaHEN at all** would therefore have read-only `/system` and
`/system_ex` and a mounted `/update`. Nothing on the PKG install path writes to those, so this is
very likely irrelevant — but it is untested, and "installs work with etaHEN killed after boot" is
not the same claim as "installs work on a console that never loaded it". That test needs a reboot.

### Our own deploy tooling was still on etaHEN too

Killing etaHEN broke deploy_elf.py — it pushed the ELF over FTP on :1337, which is etaHEN's.
A deploy tool that needs the thing you are removing is a funny kind of independence.

Both deploy paths now prefer our own /api/fs/write and keep FTP only as a recovery route for a
console whose shop ELF is not running. Measured with etaHEN dead: **42 MB in 2.5 s**, against ~6 s
over FTP. deploy.py app does the same for the UI bundle.

### The connection light was lying in the other direction

With etaHEN gone the companion reported connected: false while installs worked perfectly,
because "install ready" still meant "something is listening on :12800". Readiness now asks OUR
engine: engine: "pms-spawn", engine_ready = Payload Manager reachable, which is the only
thing it needs. dpi_* is untouched underneath for anything still reading it, and the UI status
line says *install-ready (our engine)* instead of *connected (DPI off)*.

### Where independence stands

| | |
|---|---|
| Install engine (games, updates, DLC) | **ours** |
| Console-local / USB installs | **ours** |
| File access on the console | **ours** (`/api/fs/*`, FTP only as fallback) |
| Fake-signed package support | kstuff, autoloaded — never was etaHEN's |
| `:12800` / `:9090` / klog | nothing of ours calls them |

etaHEN is **not in autoload**, so it stays gone across a reboot unless deliberately loaded. Our ELF
still ships it (`payload_bundle.h`, port 0 = ship, never auto-start) at
`/data/pkg-mutant-shop/payloads/etaHEN.elf` if it is ever wanted back.

---

## [3.27.0] — 2026-08-25 · "Queue hygiene that matches how our engine actually works, and our logo on the toast" `[VERIFIED]`

### Multi-install: what actually needs cleaning, and what does not

The third-party host needed `/cleartmp` between queued jobs because it is a **long-lived daemon**:
it accumulates scratch files, and it wedges after a heavy install so the next job has to reload it
first. That is why the queue grew `_dirty_hosts`, `recover_dpi()` and a cleanup step.

**Our engine has neither problem, by construction.** Each install is a fresh process that makes one
call and exits. There is no daemon to wedge, no port to unstick, and nothing accumulates. So the
honest equivalent of "clean the port between installs" is not a port at all — it is the two
handover files and a guarantee that two installs are never in flight at once:

- **`/api/engine/spawn-cleanup`** removes `installer-req.txt` and `installer-res.json` and releases
  the busy latch. The companion calls it after **every** spawn install, success or failure, so the
  next job in the queue cannot possibly read the previous one's request or verdict.
- **`/api/engine/spawn-status`** reports whether an install is in flight.
- **A busy latch** rejects a second concurrent spawn with `409`. Both installs would otherwise share
  one request file and the second would install whatever the first asked for. The latch **expires
  after 600 s** so a spawned process that died without writing a result can never block the queue
  permanently.
- The companion waits for `busy` to clear before handing over, on top of the existing
  per-console serialisation.

**Verified — three back to back, queued together:**

```
 0.6s Castle Crashers Remastered  submitting → transferring → promoting → playable   11.1s
11.1s Riptide GP2                 submitting → transferring → promoting → playable    9.2s
20.3s inFAMOUS Second Son JOS DLC submitting → promoting → "Update installed"         5.6s

#234  1036  title='PKG MUTANT SHOP'  CASTLECRASHERSNA   227540992/227540992
#235  1036  title='PKG MUTANT SHOP'  RIPTIDEGP2PS4001   107806720/107806720
#236  1026  title='PKG MUTANT SHOP'  UNLOCKVESTJOS000     1048576/1048576

after the queue:  busy=false · handover files: NONE · etaHEN log lines: 0
```

### Our logo on the console's own notification

`sceKernelSendNotificationRequest` takes an icon URI, so the toast can carry our artwork instead of
the generic system glyph.

**It is the ICON, not the LOGO** — and the distinction matters. `web/assets/icon0.png` is the square
512×512 mark; `web/assets/logo.png` is the wide wordmark and would be cropped to nothing in a square
notification slot. The shop ELF already extracts `web/` on every boot, so the icon is always present
and always matches the running build; the installer checks `access()` before setting it, so a missing
file degrades to a plain toast rather than a broken one.

The wording changed too. It now names the game and says what is happening **right now**:

```
  PKG MUTANT SHOP
  Castle Crashers Remastered is downloading now
```

Not "installed" — the console posts its own *"Ready to play"* when the download actually finishes.
Claiming completion at hand-off is the same lie this project has removed everywhere else.

`MetaInfo.content_name` now carries the real game name as well, so the console's own Downloads entry
reads properly instead of showing our app's name for every job.

### Also

- **Removed `/data/pkg-mutant-shop/allow-diagnostics`**, which was left enabled on the console after
  the credential experiments. The install-capable diagnostics are gated again.

### Where etaHEN stands now

Nothing on the install path uses it. It is the FTP **fallback** behind `fs_*` (ours preferred, 6–7×
faster), and it still owns `:12800`, `:9090` and the klog port — front doors nothing of ours calls.
Whether those can simply be switched off depends on what etaHEN provides *passively*, which is under
investigation separately.

---

## [3.26.3] — 2026-08-25 · "Spawn mode is the default, and the log has no etaHEN in it" `[VERIFIED]`

`dpi.mode` is now **`"spawn"`**. Every install the app performs — base games and add-ons — goes
through our own engine. **Zero etaHEN contact across an entire session log.**

### The queue no longer babysits a daemon we do not use

Flipping the config was the small part. The queue's pre-flight was written around a third-party
daemon and keyed on `mode != "pms"`, so `"spawn"` inherited all of it: `ensure_dpi_host()` reviving
whatever owns `:12800`, `ensure_dpi_ready()` probing it, `_dirty_hosts` wedge-healing,
`ensure_install_host_safe()` rewriting its config, `last_install_via()` reading Elf Arsenal's
`last-install.json`, and `dpi_cleanup()` poking etaHEN's `/cleartmp` after every job.

All of it is now behind one predicate, `ours = mode in ("pms", "spawn")`. Running that machinery in
a mode whose entire purpose is not needing etaHEN would have made our own lane depend on etaHEN
being up.

Two call sites had to be gated, not one — the first pass missed the `if installed:` branch, and the
log proved it: `[dpi-cleanup] SUCCESS: Deleted 0 temporary files.` kept appearing after installs
that never touched etaHEN.

### Verified end to end, through the app's own queue

```
Castle Crashers Remastered   submitting → transferring 2→54% → promoting → playable   11.5s
Riptide GP2                  submitting → transferring 8→79% → promoting → playable    9.3s
inFAMOUS Second Son (DLC)    submitting → promoting → "Update installed"                6.5s
```

Both games were deleted beforehand. The proof is the `bgft.db` `title` column — the only field that
records **which lane ran**:

```
#231  status=1036  title='PKG MUTANT SHOP'  UP2015-CUSA14409_00-CASTLECRASHERSNA  227540992/227540992
#232  status=1036  title='PKG MUTANT SHOP'  EP0786-CUSA02365_00-RIPTIDEGP2PS4001  107806720/107806720
#233  status=1026  title='PKG MUTANT SHOP'  UP9000-CUSA00223_00-UNLOCKVESTEXIST0    1048576/1048576
```

`1036` = full title install, `1026` = add-on. The add-on is registered in `addcont.db`. Five files
on disk for each game at the exact package size. And:

```
grep -icE "dpi-cleanup|dpi-reload|12800|etahen"  pms.log   ->   0
```

### A test that lied, and why it is worth writing down

The first run after the flip reported `playable` for both games — and the bgft rows said
**`etaHEN DPIv2`**. The frozen exe reads `config.json` from **beside itself**
(the folder the exe sits in), not from `companion/config.json` in the repo. The flip
had gone into the repo copy, which the running app never reads.

Two green "Ready to play" rows, and the lane was unchanged. This is exactly why the `title` column
gets checked instead of the queue state: **the queue reporting success says nothing about which
engine produced it.**

### Still etaHEN's

Nothing on the install path. It remains the FTP fallback behind `fs_*` (ours is preferred and
measured 6–7× faster), and it still owns `:12800`, `:9090` and the klog port — front-end services
we do not need but have not yet replaced. Turning etaHEN off entirely is now a question of those
ports, not of installing.

---

## [3.26.0] — 2026-08-25 · "SOLVED: our own engine installs base games" `[VERIFIED]`

**`0x80B2116F` is caused by calling `sceAppInstUtilInstallByPackage` from a payload INJECTED into a
hijacked host process. The identical call from a freshly SPAWNED process succeeds — with our own
ordinary homebrew authid.** Credentials were never the issue.

### How it was cornered

**1. The arguments were eliminated by a control nobody had run.** The same local path
`/user/data/tmp/pms_riptide_CUSA02365.pkg` was handed to both callers within the same minute. Ours
returned `0x80B2116F`; etaHEN returned `SUCCESS`. (etaHEN's DPI v2 sets `arg1.uri = url_value`
verbatim with no validation, so POSTing a local path as the `url` field makes it perform *exactly*
our call.) Identical bytes in, different result out.

**2. The entire ucred was eliminated.** A new read-only `/api/engine/procdiff` dumped every readable
process field for both. The differences were:

| field | ours | etaHEN util (succeeds) |
|---|---|---|
| `authid` | `0x4801000000000013` | `0x4800000000000006` |
| `ruid/svuid/rgid/svgid` | 0 | 1 |
| `jaildir` | `0xffffb84801d82ee0` | `0x0` |
| `caps` | `ff × 16` | `00000000701c004000ff0000000000c0` |
| `attrs` | *identical* | *identical* |

Matching **all of them at once** — including clearing `jaildir` and adopting etaHEN's exact
capability mask — still returned `0x80B2116F`.

**3. A spawned process succeeds.** `pms-installer.elf` (`ps5-app/onconsole/installer_probe.c`) does
nothing but the same call. Loaded by Payload Manager so it becomes its own process, it returned
`rc = 0x00000000` — while running as `0x4801000000000013`, the plain homebrew authid.

### Proven on a genuinely absent base game

`Castle Crashers Remastered` (CUSA14409, 227,540,992 B), deleted beforehand, installed from a plain
HTTP URL served by our own companion:

```
bgft #222   title='PKG MUTANT SHOP'   status=1036   227540992/227540992   err=0x00000000
/mnt/ext1/user/app/CUSA14409/   app.pkg 227540992 + app.pbm + app.pbm.backup + app.json + app.xml
/user/appmeta/CUSA14409/        icon0.png 262171, pic0/pic1.png 1189365 …  (real artwork)
```

**status 1036 = full title install.** Every previous `PKG MUTANT SHOP` bgft row in this console's
history was 1026 — an add-on. This is the first base game our own engine has ever installed, and
the console fetched it over HTTP from us, exactly as it does for etaHEN.

### What this retires

- **The authid theory** (3.24.7). Our authid was the ordinary homebrew one and it worked.
- **The "content-selective, therefore not process-global" argument** that a verification pass used
  in 3.25.4 to kill the spawned-process idea — and which this changelog repeated. **It was wrong.**
  A process-global property that ShellCore consults *only* when building the APP actor produces
  exactly the add-ons-work/games-fail split. The argument is retired; do not reuse it.
- Every argument-level theory: struct sizes, `content_type`/`content_platform`, the big-app
  announce, URI form, target presence.

### The lane, and why it is shaped this way

`GET /api/engine/install-spawn?uri=<http url or local path>` →
`GET /api/engine/spawn-result` for the verdict.

The ELF writes the URI to `/data/pkg-mutant-shop/installer-req.txt`, clears any previous verdict,
extracts `pms-installer.elf` **straight from the bytes embedded in itself** (rather than trusting
`payload_bootstrap`, whose thread waits out the user-configurable autostart delay and so has not
run on a fresh boot), mirrors it into Payload Manager's own payload directory — because
`/loadpayload` resolves by **basename** against that directory, so a full path elsewhere can
silently run an older copy — and asks Payload Manager to spawn it.

The protocol is files on purpose: no port, no parser, nothing to wedge.

`Ps5Bridge.install_spawn()` drives it from the companion and is selected by `dpi.mode = "spawn"`.
`ok` means the console accepted the package and created a BGFT task; completion is still proven
from `bgft.db`, exactly as for etaHEN. A refusal is pre-BGFT, so nothing is left half-queued.

Verified through the companion:

```
Ps5Bridge.install_spawn(http://10.0.0.76:8710/library/Castle-Crashers-Remastered-CUSA14409.pkg)
  -> ok=True  host=pms-spawn  via=spawned-process  rc=0x00000000
     content_id=UP2015-CUSA14409_00-CASTLECRASHERSNA
```

### On the notifications

This also settles the screenshots. **"Castle Crashers Remastered — Ready to play"** with the box
art is ShellCore/BGFT's own toast, emitted because the `subtype=6` task exists — there is no code
in etaHEN that produces it, and we get it for our own installs. The only line that was ever etaHEN's
is its debug toast; ours now says `PKG MUTANT SHOP — Install task created`.

### Still to do for full independence

The install *call* is ours. Serving our own `:12800`/`:9090` DPI front-ends and a klog port is
front-end work on top of a lane that already works, and `dpi.mode` still defaults to `"v2"` until
the spawn lane has more hardware time behind it.

---

## [3.25.4] — 2026-08-25 · "etaHEN's install call is our install call" `[VERIFIED]`

### We read etaHEN's source. The call is byte-for-byte ours.

Fetched from `github.com/etaHEN/etaHEN` (GPLv3; this project is GPLv3, so licence-compatible
reuse). **Note the public tree self-identifies as 2.5B and its firmware tables stop at 8.20 — it is
older than the binary on this console.**

`Source Code/util/include/common_utils.h:54-88` versus our `server.c`:

| | etaHEN | ours |
|---|---|---|
| `MetaInfo` | 6 × `const char*` = **48 B** | identical |
| `SceAppInstallPkgInfo` | `content_id[0x30]; int content_type; int content_platform` = **56 B** | identical |
| `PlayGoInfo` | `language_t[30]` + `playgo_scenario_id_t[64]` + `content_id_t[64]` + `long[810]` = **9984 B** | identical |

And `DirectPKGInstaller.cpp:1159-1204`, its DPI v2 path — the one our companion drives:

```c
MetaInfo arg1 = { .uri = "", .ex_uri = "", .playgo_scenario_id = "",
                  .content_id = "", .content_name = "etaHEN DPIv2", .icon_url = "" };
arg1.uri = url_value;                                  // the http URL, straight through
memset(&arg3, 0, sizeof(arg3));                        // PlayGoInfo zeroed
install_result = sceAppInstUtilInstallByPackage(&arg1, &pkg_info, &arg3);
```

That is our call, argument for argument. Confirmed on top of that:

- **etaHEN has no base-game branch at all.** One unconditional call handles game, patch and add-on.
  There is no `subtype` anywhere, no content-type discrimination, no special case.
- **etaHEN never touches an app-slot API.** No `GetPrimaryAppSlot`, no
  `sceAppInstUtilGetAppEmptySlot`, no `applyPatchPrimarySlot` — those lines in the klog are
  ShellCore's own internals, not the caller's.
- **On FW 12.70 etaHEN's ShellCore patcher applies nothing** — its firmware table stops at 8.20. So
  no ShellCore patch can be the differentiator.
- It leaves `SceAppInstallPkgInfo` **uninitialised** where we `memset` it — already eliminated by
  the 18-combination sweep in 3.25.1.
- `set_proc_authid(getpid(), DEBUG_AUTHID)` is `0x4800000000000006` — exactly the value measured
  and A/B'd on hardware in 3.24.7, both per-call and set before IPC init.

**So the difference is not in the call.** 63 candidate differences were extracted from the source
and adversarially checked; **2 survived**, and the reason the other 61 died is a single observation
that is worth more than any of them:

> **The failure is content-selective.** Add-ons succeed and base games fail **in the same process,
> on the same thread, through the same code path, differing only in the URI string.** Anything
> process-global — init order, thread identity, linked library surface, `sceNetCtlInit`, authid,
> process ancestry, `elfldr_spawn` versus payload-injection — would poison **both** equally. It
> cannot select `subtype=6` and spare `subtype=7`.

That retires the whole class of environment theories at once, including the spawned-process one this
entry originally recommended. Two corollaries fell out while checking it: etaHEN's own DPI v2 calls
`InstallByPackage` on a libmicrohttpd per-connection worker (`MHD_USE_THREAD_PER_CONNECTION`), *not*
the thread that called `Initialize` — so thread affinity is demonstrably not part of the contract;
and our own add-on successes prove our lazy `sceAppInstUtilInitialize()` produces a fully working
installer session that reaches and completes pre-allocation.

The gate is on the **callee** side: whatever ShellCore consults for the APP actor and not for the AC
actor — the application-slot resolution the add-on path never touches.

**The two survivors**, both cheap and testable in one build: `MetaInfo.content_name` (etaHEN sends
`"etaHEN DPIv2"`, we send `"PKG MUTANT SHOP"` — weak, but the one argument never varied, and
plausibly content-selective since an app entry needs a display name and an add-on does not), and
preloading `libSceNetCtl`/`libSceSysmodule`/`libSceNet`/`libSceVideoOut` plus `sceNetCtlInit()`
(rated mechanistically dead for the netctl half, since ShellCore already fetched the package in its
own process — free to add, low expectation).

### The notifications, settled

The **"Castle Crashers Remastered — Ready to play"** toast with the game's artwork is **not
etaHEN's**. There is no code anywhere in etaHEN that emits it. It is **ShellCore/BGFT's own**,
produced as a side effect of the `subtype=6` task existing. We already get it today, because our
installs go through BGFT.

The only thing on screen that belongs to etaHEN is its debug toast, `[etaHEN] DPI: Direct install
console Task started for <url>` — emitted by `notify()` right after the call returns
(`DirectPKGInstaller.cpp:1205`). When our own engine creates the task, that line becomes ours and
the console's own notifications are unchanged.

---

### Audit items closed this build

**[11] A four-minute stopwatch was issuing verdicts.** After the last byte landed, the confirm loop
failed the job at 240 s with *"Console rejected this package — delivered in full, but it won't
install (bad dump)"* — an accusation about the file, produced by a clock. A large title can spend
far longer than that copying and decrypting, and a brief FTP hiccup makes `installed_titles()`
return `None`, which reads as "not there yet". It now waits on **evidence**: it keeps going while
the console's own bgft row is still changing, fails immediately on a terminal non-success status
(`BGFT_FAILED`, e.g. the `1021 / 0x80B21104` observed here), and otherwise gives up only after
30 minutes of no movement — reporting *that*, not a guess about the dump.

**[32] `installed.json` was unlocked read-modify-write.** Two workers finishing together each read
the same list; the second write lost the first title and the badge reverted. Now guarded by an
`RLock` with the re-read **inside** the lock. Proven: 200 concurrent `remember_installed` calls,
**0 lost**. It also no longer assumes the file's shape — a bare list, `{"installed": null}`, or
unparseable junk each used to raise inside the outer `try` and silently drop the write.

**[15] A null or wrongly-typed config section bricked startup.** `POST /api/config` deep-merges an
unvalidated body, so `"companion": null` or `"port": "eight thousand"` produced a `TypeError`
before anything was logged — in the frozen exe, a window that closes with no message. `coerce_config()`
now restores any top-level section that is not the right shape, coerces `companion.port`, and says
what it repaired. Applied on load **and** on the live-update path.

**[27] / [30] One byte-counter per library file, shared by every console.** "Install to all
consoles" on one game: both jobs credited and read the same `transfers[key]`. Console A at 40 GB,
console B's hand-off resets it to 0 — A's stall detector starts counting while B displays A's
percentage and drops into "promoting" the moment A finishes. Two jobs, one number, both wrong. The
counter is now bucketed by the requesting console's IP, with the bare-key entry kept as the
aggregate because that is what a peer companion polls via `/api/served/<key>`.

### Verified on hardware

`Riptide GP2` reinstalled through the app on this build: `submitting → 8% → 28% → 33% → promoting →
playable` in 5.0 s, five files at `/mnt/ext1/user/app/CUSA02365/` with `app.pkg` at exactly
107,806,720, and `/api/served/` still reporting the aggregate correctly for peers.

---

## [3.25.3] — 2026-08-24 · "Eighteen call sites down to four, and the exe finally keeps a log" `[VERIFIED]`

### FTP is no longer a dependency — it is a fallback

Every console read and write now goes through **our own on-console server first**, with the
third-party FTP kept underneath as a fallback. FTP appears in exactly **four** places in the
companion — inside `fs_read`, `fs_list`, `fs_write` and `fs_mkdir` — down from 18 scattered call
sites. Converted this round:

`ensure_install_host_safe` · `last_install_error` · `last_install_via` · `register_path` ·
`ensure_register_path` · `installed_app_pkg` · `cheat_library_status` · `sync_cheat_library` ·
`console_backups` · `console_icon` · `console_cheats` · **and the mount lane's upload**.

New primitives: `fs_mkdir`, and `fs_text` (most callers were decoding the same small config or
json by hand). `fs_write` now takes a file object plus `size` so urllib streams it straight off
disk — the difference between being usable for a 90 GB game and only for small files.

**Both transports, every converted path, measured on hardware — identical results:**

| | ours | FTP | |
|---|---|---|---|
| `console_apps` | 1.07 s | 6.61 s | 136 titles both |
| `console_ps4_appmeta` | 0.84 s | 4.44 s | 55 PS4 titles both |
| `install_job_row` | 0.02 s | 0.17 s | same row |
| `console_backups` | 0.11 s | 0.33 s | 52 backups both |
| `cheat_library_status` | 0.22 s | 0.52 s | reachable both |
| 40 MB streamed upload | 2.57 s (16.3 MB/s) | 6.05 s (6.9 MB/s) | byte-identical both |

**Both transports are now atomic.** The FTP branch of `fs_write` writes `<name>.part` and renames,
matching what the ELF already did. Writing straight to the final name leaves a partial container in
ShadowMount's scan folder, where it looks exactly like a real game — half a game that mounts, or
refuses to, for no visible reason.

### A bug this found, in the code it replaced

Cancelling a mount upload **did not cancel it**. `fs_write` caught the progress reader's
`_Cancelled` in its blanket `except Exception`, concluded our transport was broken, marked it dead
for 20 s, retried the whole transfer over FTP, and finally returned `False` — so the queue reported
*"Upload failed"* for a job the user had deliberately cancelled, **and left a `.part` file behind in
a watched folder**. A cancel is not a transport failure. Both branches now re-raise it, and the FTP
branch deletes its own `.part` on the way out.

Verified on hardware, cancelling a 40 MB upload at 4 MB:

```
our file API : _Cancelled propagated in 0.26s · transport still trusted · no files left
FTP fallback : _Cancelled propagated in 2.61s · no files left
```

### [16] A corrupt config.json used to overwrite itself with defaults

`config.json` truncated by a power cut or an editor crash was silently replaced by `DEFAULT_CONFIG`
in memory. The defaults point at `192.168.1.50`, so the console was unreachable, so `main()` took
its auto-discover branch — **and called `save_config()`, writing the defaults over the user's
file.** Library paths, sources and console list gone, with nothing said.

`load_config()` now records that the file could not be parsed, keeps a timestamped copy
(`config.json.unreadable-YYYYmmdd-HHMMSS`), and `save_config()` **refuses to write** while that
flag is set unless called with `force=True`. `/api/health` carries `config_unreadable` so the UI
can say so rather than quietly running on defaults.

Proven by feeding it a truncated file: flagged unreadable · ran on defaults · save refused · **the
user's file byte-intact** · copy kept · `force=True` still works.

### [17] The shipped exe kept no log at all

Every diagnostic here is a `print()`, and the exe is built `--noconsole`: stdout is a null device.
So when an install failed, the host's real reply — the SCE error code, which FTP port answered,
whether a reload was even attempted — was printed and discarded. The user had a one-line UI message
and no way to learn anything else.

There is now `pms.log` beside the exe: timestamped, flushed per line (a crash must not swallow the
last lines), rotating one generation at 2 MB. `print` is *wrapped* rather than replaced at every
call site, so ~200 existing diagnostics became useful immediately and stdout still works from
source. Confirmed writing on first boot.

### End to end, on hardware

`Castle Crashers Remastered` installed through the app's own button on the fully converted stack:

```
0.0s submitting · 7.1s transferring 2% · 9.2s 28% · 10.7s 83% · 11.4s promoting · 12.1s playable
/mnt/ext1/user/app/CUSA14409 → app.pkg 227,540,992 (= the exact package size) + the four sidecars
installed titles 136 → 137
```

Real byte progress the whole way, and the "Ready to play" was a bgft-confirmed verdict.

### What still uses etaHEN

**One thing: installing a base game.** Add-ons already install through our own engine; every file
operation is now ours with FTP only as a fallback. Once you are satisfied with the fallback path,
etaHEN's FTP can be switched off entirely without the app noticing.

---

## [3.25.1] — 2026-08-24 · "Our own filesystem, and the last caller-side theory dies" `[VERIFIED]`

### The app can now read and write the console without anybody else's FTP

Every console read in the companion went through FTP — **18 call sites** — and FTP on this console
belongs to etaHEN (`:1337`) or to `ftpsrv` (`:2121`). Neither is ours. That was a hard dependency on
software we do not ship, for the sake of reading files on a machine where **our own payload already
runs as root with an HTTP server on `:8710`**.

The ELF gained a filesystem API: `GET /api/fs/read|list|stat|mkdir|delete` and a **streamed**
`POST /api/fs/write`. The write is handled straight off the accept loop, before the generic POST
path, because that one reads a body into an 8 KB stack buffer — fine for JSON, useless for a game.
It writes `<path>.part` and renames on success, so a broken transfer can never be mistaken for a
finished file — and in ShadowMount's watch folders can never be auto-mounted half-written.

The companion gained `fs_read` / `fs_list` / `fs_write` / `_pull_db`, which **try our own server
first and fall back to FTP**, remembering which answered. Nothing regresses if the ELF is not
loaded: the old path is still underneath, unchanged.

Converted so far — the four database pulls (`app.db`, `bgft.db` ×2, `addcont.db`) and the two
directory consumers (`console_ps4_appmeta`, `title_has_mount_link`). Those are the install-confirm
and install-detection paths, i.e. the ones that run constantly.

**Measured on hardware, both transports, identical answers:**

```
bgft.db read        HTTP 0.011s   FTP 0.079s     7.3x   byte-identical (sha256)
install_job_row     HTTP 0.027s   FTP 0.070s     2.6x   same row
PS4 appmeta scan    HTTP 0.91s    FTP 6.19s      6.8x   same 54 titles
24 MB streamed write         1.15s  (21.8 MB/s)  byte-identical round trip, no .part left behind
```

It is faster because FTP pays a connect, a login and a data-channel setup for **every single
fetch**, and the confirm loop re-reads `bgft.db` every few seconds for the whole of an install.

On security this is a **smaller** exposure, not a new one: etaHEN's FTP already grants any host on
the network anonymous root access to the entire filesystem, with no origin check and no way to turn
it off without losing the install host. These routes sit behind the origin guard from 3.24.8 — and
once the remaining writers move over, etaHEN's FTP can simply be switched off.

**Still on FTP:** the mount-lane upload, the cheat sync, and `console_backups`. `fs_write` already
accepts a file object plus `size` so it streams off disk rather than buffering, which is what those
need; they are the next conversion, not a blocker.

### A bug this introduced, and caught

Routing the four DB pulls through `_pull_db` left three call sites doing `self._drop_tmp(tmp)` with
`tmp = None` on the failure path. `os.remove(None)` raises **TypeError**, which `except OSError`
does not catch — so a momentary console hiccup would have propagated out of `install_job_row`
instead of returning `None`. `_drop_tmp` is now None-tolerant and the redundant calls are gone.

Converting `console_ps4_appmeta` also left its tail reading `p[4]`, a variable from the deleted FTP
parse. The resulting `NameError` was swallowed by the function's own `except`, so **PS4 detection
silently returned zero titles** — on both transports, which is exactly why "same answer as FTP"
looked like a pass. Caught because 0 PS4 titles is absurd on a console with 54 of them. It now
reads `e["size"]` from the listing and finds all 54, on both transports.

### The last caller-side hypothesis is dead too

3.24.7 eliminated the caller's *identity and context* for the base-game blocker (credentials, both
before and after IPC init; the big-app announce; target presence; storage; a missing Sony call).
What remained was the caller's *arguments*: `SceAppInstallPkgInfo{content_id, content_type,
content_platform}` has always been passed as **all zeros**, on the assumption that it is purely an
OUT parameter. Zero is proven to work for an add-on. Nobody had ever varied it for an app.

Swept 18 combinations of `content_type` × `content_platform` against an **absent** base game
(Riptide GP2, deleted from the console for the purpose):

```
0/0 1/0 2/0 3/0 4/0 5/0 6/0 7/0 8/0 0/1 1/1 2/1 0/2 1/2 2/2 6/1 6/2 7/1
  -> 0x80B2116F on every single one, content_id empty on every single one
```

Uniformly negative. The struct fields are OUT-only in practice. **Caller-side is now genuinely
exhausted** — identity, context and arguments. The remaining question is entirely on the callee
side, and the honest next step is reading etaHEN's GPLv3 source around `DbgInstall` / app-slot
resolution rather than another A/B from here.

### End-to-end regression, on hardware

`Riptide GP2` reinstalled through the app's own Install button on the new code:
`playable / 100% / "Ready to play"`, five files at `/mnt/ext1/user/app/CUSA02365/`
(`app.pkg` 107,806,720 = the exact package size, plus `app.pbm`, `app.pbm.backup`, `app.json`,
`app.xml`), and `installed_titles` back to 136 including it. The `/api/engine/*` diagnostics were
switched back off afterwards and verified 403.

---

## [3.24.9] — 2026-08-24 · "Buttons that discard what the server said" `[VERIFIED]`

More of that audit worked through. Everything here is a defect that changed what the user
saw or lost their work — no refactors.

### "Clear finished" deleted the queue

It kept a whitelist (`RUNNING` + `queued`) and deleted everything else — which includes every
**held** job. Build a queue with ＋ Queue (all held), press *Clear finished* to tidy one failed row,
and the entire batch is gone server-side. It is worse right after a wedge, where `_pause_pending`
deliberately holds every pending install: the one button offered to tidy up is the one that throws
the batch away. It now drops only genuinely terminal states, which is what the button says.

`"reloading"` was in the same blast radius and is now also in `RUNNING` — a reloading task has a
worker inside `ensure_dpi_ready()` for up to 75 s, and leaving it out let `_claim()` start another
job past `max_parallel`.

### A multi-part install wrote several HTTP responses into one socket

`_install()` recurses into itself once per part, and **every one of those calls wrote a complete
HTTP response**. A 3-part release wrote four responses to a single request: the client reads the
first — part 1's — and the rest are garbage on the wire. The `{"multi_part":true,"parts":3}` reply
that was supposed to be the answer went into a socket nobody was reading.

The recursion now collects instead of writing (`_json` mutes and returns its payload), so exactly
one response is emitted — and each part's real result is finally honoured. Previously a part that
was **refused** or **already installed** was counted as queued regardless; now a refusal reports
which part failed and why, and a release whose parts are all present reports that instead of
queueing nothing and claiming success.

### A held job kept the address the PC had when it was queued

Queue ten games with ＋ Queue in the evening; overnight DHCP moves the PC, or Windows switches from
Ethernet to Wi-Fi. In the morning every job hands etaHEN a URL on an address that no longer exists.
Jobs that point at us are now flagged `relan` and their URL is rebuilt **at submit time** with
`companion_ip_for(bridge.ip)`, logging the change when it happens.

Same class of bug in `start_pc_register_thread`: it announced one `lan_ip()` to every console,
while the install URL used `companion_ip_for()` per console. On a dual-homed PC (Ethernet on the
console's subnet, Wi-Fi holding the default route) the console was told to reach us on an address
it may have no route to — and the console's own UI then pointed at the wrong PC while installs
worked fine. Now resolved per console, with the same helper.

### Reinstall could never reinstall

*Reinstall base* — both the drawer button and the ⋯ menu item — hit the server's already-installed
guard and came back as a green "already installed" toast with the button relabelled. There was no
way to reinstall a short or corrupt game from the UI at all. The server has implemented `force` as
the documented override the whole time and nothing passed it; both deliberate-reinstall entry
points now do. (Install and ＋ Queue are unchanged — they still refuse duplicates.)

### The UI threw away every explanation the server sent

Only `base_not_installed` was translated; everything else rendered as `"Install failed: " + code`.
So a split release with a part missing produced *"Install failed: missing_part"* while the server
had already written a full sentence saying which part and why. The toast now prefers the server's
own `message`.

Related: a multi-part install said "Sent to PS5 · Game" and hid `"Queued 3 parts"`, so the extra
amber held rows and the *Start queue (N)* button arrived unexplained. It now reports the parts.

### A fully paused queue animated as "N installing"

A wedge holds every pending install; the dock counted them as live and spun an amber pill reading
"3 installing" while the console was doing nothing at all, each row telling the user to press Start.
`held` is now split out of `live` and reads "N waiting to start", not spinning.

### The version tag hid a stale UI

`#verTag` showed whichever of (companion, this page) was **newer**. A 3.24.2 UI talking to a 3.24.9
companion therefore displayed 3.24.9 and looked current — the exact situation worth seeing, hidden.
It now always shows this page's version, and on a mismatch appends `(PC vX)` in the warning colour
with a tooltip saying which side to update.

### `deploy.py app --companion` has never survived a reload

`extract_web()` rewrites every bundled file to `WEB_ROOT` on each ELF boot — including `config.js`,
the one file there that is *configuration* rather than build output. So the companion URL that flag
writes was truncated back to the shipped comment on every load and `window.PMS_API` was always
undefined. `config.js` is now shipped only when it is missing or empty.

Proven on hardware: wrote a marker into `config.js`, reloaded the ELF, and the marker was still
there afterwards. (Left at the auto-discovery default deliberately — a hardcoded PC address here is
the same failure the two fixes above just removed.)

### Also

- `recover_dpi()`'s on-console fallback read `cfg["server"]["port"]`, a key that **does not exist**.
  It has always fallen through to the 8710 default, which happened to match — until anyone sets
  `console.shop_port`, at which point every other `_shop` call follows it and this one alone does
  not. Now reads the same key as everything else.
- Two wedge messages named **"Elf Arsenal / ps5-dpi-v2"**, payloads this build does not ship, so the
  user went looking in Payload Manager for software that is not there and never tried etaHEN, which
  is the actual fix. Both rewritten to name the real host and the real action.

---

## [3.24.8] — 2026-08-24 · "The console stops taking orders from strangers" `[VERIFIED]`

### The on-console server had no idea who was talking to it

It answers on `INADDR_ANY:8710` with `Access-Control-Allow-Origin: *`, **as root**, and some of its
routes install packages and write process memory. The PC companion has had an origin guard since it
grew a web UI; this end never did, so any page open in any browser on the network could drive it.

`request_origin_ok()` now mirrors the companion's rule exactly, applied in the accept loop before
anything is dispatched. The test is **not** "same origin" — the console serves its UI to itself
while the data comes from a PC on a different address, and that *is* the architecture. The test is
whether the requesting page has a DNS hostname at all: every legitimate caller is a private-range
IP literal or loopback, and a hostile public site always has a name. A request with **neither**
header is allowed on purpose — that is curl, the companion, and our own tooling, none of which a
remote page can forge.

Verified on hardware:

```
no Origin (the companion)          -> 200
Origin: http://10.0.0.99:8710      -> 200   (the console's own UI)
Origin: http://10.0.0.76:8710      -> 200   (the PC's UI, cross-origin by design)
Origin: https://evil.example.com   -> 403
```

### Four routes that install packages are now opt-in

`/api/engine/install-inproc`, `install-local`, `install-dir` and `diag` call `libSceAppInstUtil`
directly. Nothing in the UI and nothing in the companion calls them — they are hand-fired
diagnostics, and `diag` performs **two real installs** of the same package. They stayed reachable
by anyone on the LAN purely because nothing had ever said otherwise.

They now require `/data/pkg-mutant-shop/allow-diagnostics` to exist, and answer
`403 {"ok":false,"error":"engine diagnostics are off …"}` when it does not. **The product's own
lane (`/api/engine/install-url`) and every read-only route are not gated** — verified:
`/api/engine/authid` and `/api/engine/job` still answer normally.

### Console-local progress was the constant 50

`/api/install/status` reported `pct = 50` for the entire install. On a 90 GB package from a USB
stick that is twenty minutes of a bar that never moves — indistinguishable from a hang, and exactly
what makes someone power-cycle a console mid-write.

We genuinely cannot know the figure there: etaHEN does the fetching and reports nothing back. So
the honest answer is **unknown**, not a number that looks like measurement. `pct` is now `-1` for
"in flight, unmeasurable", and the queue row draws a sweeping indeterminate bar labelled *working*
instead of a stalled percentage. (The PC companion **does** know — it reads real byte counts out of
`bgft.db` — and its rows are unchanged.)

### A console that goes away no longer destroys the queue

Ten games queued, the PS5 reboots after the third: every remaining job cost about two seconds to
discover the console was gone, so the whole queue burned down to `error` in under half a minute and
each one needed re-queueing by hand. The offline branch now **holds** and pauses the rest of that
console's queue, exactly as the wedge path has since B8 — being switched off is no different, and
far more likely.

### A transient etaHEN restart no longer holds the queue for good

`ensure_dpi_ready()` allowed about eleven seconds in total (one 5 s probe plus `recover_dpi`'s
single 6 s check) before declaring the lane wedged. etaHEN rebinds `:12800` on its own retry loop,
so a DPI v2 toggle, an etaHEN restart, or a momentarily busy console exceeds that easily — and the
queue then held permanently, with nothing to release it. An unattended overnight run of eight games
stopped after the first. It now waits up to 75 s for the lane to come back on its own, reporting
`ready (came back on its own after Ns)`, before reloading anything. `recover_dpi`'s own success
path has always looped this way.

---

## [3.24.7] — 2026-08-24 · "The A/B nobody had, and the end of the authid theory" `[VERIFIED]`

### The measurement

`Riptide GP2` (`EP0786-CUSA02365_00`, 107.8 MB) was installed **twice on this console nine minutes
apart** — once by our in-process engine (failed) and once by etaHEN through the app's own Install
button (succeeded, bgft #213 status 1036, 2798 ms). Both klog traces captured live on `:9081` and
preserved at `research/klog-authid-ab-2026-08-24.txt`.

Before this, the only "working install" capture in the repo was an **add-on** —
`grep -o "subtype=[0-9]*"` on `klog-working-install-2026-08-23.txt` returns `3 × subtype=7`. Nobody
had ever watched etaHEN install a base game, so every conclusion about base games had been drawn
from a trace that did not contain one.

The two traces are **byte-identical** through `GetRawContentInfo 0x0`, `applyPatchPrimarySlot
0x80a30004`, `AppPrepareOverwriteByPackage 0x0`, `GetPrimaryAppSlot 0x80a3000e`, `DbgInstall begin`
and the storage enqueue. They diverge on exactly one line: etaHEN reaches
`[DbgInstall] Staring Pre-allocation transfer` and gets a `[type=1, subtype=6]` BGFT task; we
reserve **zero bytes** (`m.2 free` unchanged across enqueue) and get `0x80B2116F`.

Our successful add-on installs log `Staring Pre-allocation transfer on NativeAcPackageActor`. The
base-game line names no actor. **We can build the add-on actor and not the app one, and the refusal
lands exactly there.**

### Two hypotheses closed

**"The title is already installed, so it takes the overwrite branch"** — dead. Riptide had been
deleted since the last test, so this attempt fired at an **absent** title.
`AppPrepareOverwriteByPackage` is taken anyway and returns `0x0`, and the failure is identical.
Presence is irrelevant; that call is just the APP path.

**"We are calling with the wrong authid"** — dead, and it was the last open caller-side idea
(3.24.0: *"We have never tried `...006`. That is the next experiment."*). It was first **measured**
rather than assumed, with a new read-only probe:

| pid | process | authid |
|---|---|---|
| 381 | **etaHEN Utility Daemon** — the one whose base-game installs work | **`0x4800000000000006`** |
| 374 | shadowmountplus.elf | `0x4800000000000006` |
| 59 | SceShellCore | `0x4800000000000010` |
| 370 / 261 / 87 / 265 | etaHEN critical, pldmgr, elfldr, kstuff | `0x480000001000000e` |
| — | **us** | `0x4801000000000013` |

The guess was exactly right. Then it was tried twice, because one of the two ways is not a real
test: `sceAppInstUtilInitialize()` establishes the installer IPC channel, so a credential swapped
*after* that may never reach ShellCore at all.

1. per-call swap, restored afterwards → `0x80B2116F`
2. **whole process, set at boot before any IPC exists** (fresh pid, `GET /api/engine/authid`
   confirmed `0x4800000000000006`) → `0x80B2116F`, **klog line-for-line identical**

Same credential as the process that succeeds, same everything ShellCore derives (`getAppId` →
`0xffffffff` for both), same refusal. Caller-side explanations are now exhausted: package format,
target presence, the big-app announce, storage, a missing Sony call, and credentials have all been
eliminated by experiment. The remaining candidate is the **callee** — etaHEN patches the kernel and
ShellCore, and the difference may be a patch that only helps while etaHEN is itself the caller.

**Practical consequence, unchanged and now firmly evidenced:** our own engine is production-ready
for **add-ons** (updates and DLC, PS4 and PS5, in-process, no etaHEN) and **cannot install base
games**. etaHEN stays for base games.

### Harness added — all default-off or read-only

- `GET /api/engine/authid[?pid=N]` — reads a process's authid. Writes nothing, calls nothing.
- `GET /api/engine/install-inproc&authid=0x…` — runs one call under another credential and
  **always** restores ours, on every path.
- `/data/pkg-mutant-shop/authid` — read once by `jb_escalate_pid()` at startup. Absent or
  unparseable means the long-standing `0x4801000000000013`, so a console that has never heard of
  this behaves exactly as before. The file was removed at the end of the experiment and the app
  verified back on its normal credential.

### Also in this build

- **`_run()` re-drives only after a genuine reload.** `recover_dpi()` returns
  `(True, "etaHEN install host is already answering")` because etaHEN cannot be restarted from
  here — and the two re-drive paths took that as a successful heal and **re-POSTed the package**.
  A timed-out or reset hand-off may well have reached etaHEN, so that is the duplicate-bgft-job
  hazard this project has already been bitten by. The proactive path had the guard; the two that
  actually re-submit did not. Now they share `_really_reloaded()` and hold instead.
- **An unreadable bgft snapshot no longer proves an install.** `install_job_row()` returned `None`
  both for "no row" and "could not read bgft.db", and with a `None` baseline the add-on freshness
  test (`row != before`) is passed instantly by any pre-existing completed row — so a DLC could be
  called installed three seconds after hand-off on the strength of the base game's old row. The
  snapshot now retries three times and records `_bgft_before_ok`; without a baseline the bgft test
  is skipped entirely and the job falls through to an honest timeout.
- **Console DB pulls no longer share a filename.** `bgft.db`, `addcont.db` and `app.db` were each
  fetched to one fixed path per console, so the confirm loop, the library scan and the UI could
  truncate a file another thread was mid-read on — `database disk image is malformed`, swallowed,
  and read as "not installed yet". Each pull now uses `mkstemp` and removes its own file, including
  on the FTP-failure path (which the fixed-name version could afford to leak and this one cannot).
- The `?bigapp=0` note in the 3.24.1–3.24.3 entry described the parameter's original rationale.
  It has since been A/B'd and **ruled out** — `?bigapp=0` returns the same `0x80B2116F` with an
  identical trace.

### Verified end to end on hardware

The app's own install lane was recorded state-by-state during the live etaHEN install:

```
0.0s  submitting    0%   Handing off to PS5
4.1s  transferring  8%   Downloading 8%
5.2s  promoting    90%   Installing on PS5
9.3s  playable    100%   Ready to play
```

"Ready to play" was a real verdict, not a timer: bgft #213 reached status 1036 at
107806720/107806720 and the console's installed-title count went 136 → 137.

---

## [3.24.5] — 2026-08-24 · "Buttons that cannot work, and a verdict handed down one second in" `[WIRED]`

Findings from an audit that traced **every** path in the app capable of installing a
package — the companion's HTTP dispatch and all seven queue lanes, every endpoint on the on-console
ELF, every fetch in the UI, and every config switch that changes routing. 40 paths were enumerated
and each non-etaHEN or claimed-unreachable one was then re-checked by an adversary told to refute
it. The routing conclusion is in 3.24.4; these are the defects that came out with it.

### The ⟳ "Reload install engine" button was a 404

`web/index.html` calls `api("/api/dpi/reload")`. **There is no such route on the companion.** The
GET dispatcher fell through to `_static()` and answered `404 not found`, so the toast read
"Reload failed" every time, on every console, since the button shipped — while three separate
install-failure messages instruct the user to press it ("Reload the install engine (⟳ in the
queue) and try it again").

`GET`/`POST /api/dpi/reload` now exist and run the same recovery the queue runs by itself. On
etaHEN there is nothing to relaunch (its DPI lives inside the daemon that also serves FTP and
klog), so the button does the one thing that *is* correct there — `POST /cleartmp` — and then
reports the state of the lane. It does not say "reloaded" when nothing was reloaded.

### The ELF guessed Arsenal's protocol whenever it could not identify the host

`dpi_host_is_etahen()` returned `0` for three different situations — connect failed, read failed,
and "the page says something else" — and the caller then spoke **Arsenal's** JSON API to whatever
was on `:12800`. A busy etaHEN that missed the 4-second sniff got a `POST /api/install`, an
endpoint it does not have; the connection is reset and the user is told *"no reply from the Elf
Arsenal install daemon"* about a console running etaHEN perfectly well.

It is now `dpi_host_kind()` → `DPI_ETAHEN` / `DPI_ARSENAL` / `DPI_UNKNOWN`, and UNKNOWN **refuses**
before opening the install socket, so nothing can be half-queued. The companion has refused on
UNKNOWN since 3.20.0; the two ends finally agree.

### "Installed" one second after handing the package over

The console-local lane (a PKG already on a USB stick / the console's own storage) called
`dpi_install_url()` and treated its return as the verdict. Against etaHEN that return arrives in
about a second — `SUCCESS: Task started` — and means **queued**, not installed. The install then
runs for minutes and can still fail on a bad dump, no space, or a crashed installer queue. The
console posted a PS5 notification saying *"X installed — It is ready on your home screen"*, and the
queue row went green, before a single byte had been fetched.

`/api/install/status` now carries `accepted_only`, the notification says what actually happened
("X is installing — the console is downloading it now"), and the companion waits for the same
proof the download lane demands: for an add-on or an already-registered title, **a bgft row that is
not the one snapshotted at handoff, fully transferred, with a terminal status (1026/1036)**; for a
brand-new title, **the game's own `app.pkg` at full size**. A timeout is reported as a timeout —
not as success, and not as a bad package.

### A disk-full download reported success

`http_download` called `write_all()`, which returns `void` and swallows a failed write. With `/data`
full, every write was dropped while the byte counter went on climbing, `have` reached the expected
size, the transfer "completed", and a **truncated PKG** was handed to the installer. A new
`write_all_checked()` is used on the file path only (the socket paths keep the forgiving version,
where a dropped client is not worth unwinding) and the transfer now fails with
`could not write to storage at N bytes (disk full?)`.

### Cancel did not cancel

Two halves, both wrong. A job cancelled while it sat in the queue only got a **flag** — nothing
looked at it until well after the handoff, so the worker still claimed it, still ran the preflight,
still POSTed the URL, the console really installed it, and the row then said "Canceled". `_run()`
now checks the flag before any lane can do anything. And `"submitted"` was treated as a *terminal*
state, so `cancel()` returned `False` for the one state that most needs it and the ✕ did nothing at
all, silently. A queued/held/submitted task is now marked cancelled immediately; a running one still
gets the flag, because its lane has to unwind its own transfer.

### A "submitted" job spun forever and offered no way out

`submitted` means the console was handed the package and then stopped reporting. The dock counted
it as **live**, so the pill spun that game's name forever, the count stayed at 1, and "All done"
never appeared. There was no retry button (the condition covered only `error`/`canceled`) and the ✕
was refused above. It is now excluded from `live`, shown in the warn colour as "N needs a look",
reads "… — no progress reported", and gets both buttons. `cancelTask` also surfaces a refusal
instead of swallowing it.

### Also

- The frozen exe does not carry the 48 MB cheat library (the ELF embeds all 7022 files and writes
  them itself). `sync_cheat_library()` said "no bundled cheat library at C:\…\_MEI…\cheats", which
  reads like a broken install; it now explains that the console already has them.
- `TOOLCHAIN.md` and the overhaul roadmap's invariant #1 still described **Arsenal's** protocol
  (`POST /api/install {"url":…}`, `res ∈ {0,ok,true}`) as *the* install contract. Following that
  literally now breaks installs; both now state the sniff-then-speak rule and both protocols.

### Known, deliberately not changed

- **The ELF's `/api/engine/*` endpoints bypass etaHEN and are reachable from anywhere on the LAN**
  (`:8710` binds `INADDR_ANY`, no auth). No UI or companion caller exists — they are hand-fired
  diagnostics — but `install-inproc`, `install-local`, `install-dir` and `diag` all call
  `libSceAppInstUtil` directly, and `diag` performs two real installs. Worth a decision.
- **`POST /api/config` deep-merges an unvalidated body and persists it**, so a single request can
  set `dpi.mode` to `pms`/`ezremote`/`v1` and move the whole fleet off etaHEN permanently. The
  origin guard is the only thing in front of it.
- The two `PKG-MUTANT-SHOP.exe` processes are **not** two instances — PyInstaller's one-file
  bootloader plus its child (verified by parent PID). There is still no single-instance guard.


---

## [3.24.4] — 2026-08-24 · "The mount lane was dialling a port nobody was on, and calling it success" `[VERIFIED]`

Everything here was found by probing the running console rather than by reading the code, and every
claim below was measured on 10.0.0.99 before it was fixed.

### The mount lane could not connect, and answered that by faking the whole transfer

`_run_mount` — the lane that puts a PS5 backup (`.ffpfsc`, `.ffpfs`, unpacked folders) onto the
console — was the **last place in the codebase still dialling `ftp_port` straight from config**:

```python
ftp = ftplib.FTP()
ftp.connect(bridge.ip, bridge.c["ftp_port"], timeout=15)     # 2121, from config.json
```

Every other console read (app.db, bgft.db, addcont.db, the cheat sync, the install proof) goes
through `Ps5Bridge._ftp()`, which tries **2121 then 1337** and remembers which answered. It has to:
Elf Arsenal's `ftpsrv` served 2121, **etaHEN's own FTP serves 1337**, and Arsenal is no longer
bundled. Measured on this console:

```
2121 -> ConnectionRefusedError
1337 -> 220 FTP server 1.0A for PS5 by LM.
```

So with a stock `config.json` the mount lane's connect **always failed**. That alone would be a bug.
What it did next is the real one — the `except` branch ran a **fake transfer**:

```python
except Exception:
    self._set(t, simulated=True, state="transferring", msg="Offline — simulated mount")
    ...  animate pct 0 -> 90 -> 100 ...
    self._set(t, state="playable", pct=100, msg="Deployed to ShadowMount (simulated)")
```

`state="playable"` at `pct=100`. In the queue that renders as **"Installed · est."** — a green,
finished-looking job for a game that never left the PC. That is the exact lie removed from the
install lane in 3.18.2 and again in its `console_offline` branch, left standing here.

Both halves fixed: the lane now uses `bridge._ftp(timeout=15)` like everything else, and an
unreachable FTP is reported as `state="error"` with `fail_reason="ftp_unreachable"`, naming the
ports it tried. **Nothing is animated to 100% any more.**

### ShadowMount was probed on elfldr's port — in the two endpoints that matter

3.24.x corrected `9021 -> 10101` in `payload_bundle.h`, `/api/helpers` and the boot notification,
having established that **9021 is elfldr, the ELF loader, which is always bound** — so probing it
reported ShadowMount "active" unconditionally. It missed `/api/sources` and `/api/health`, and those
are the two the settings panel actually reads, so the LED stayed permanently, wrongly green.

There is now a single `SMP_API_PORT` constant (10101 — the port ShadowMountPlus announces in its own
log) and no literals left to miss. The PC side had the wrong number too: `DEFAULT_CONFIG`,
`helper_status()`, `config.json` and two spots in `index.html` all defaulted to 9021.

### FTP: three more consumers that only knew about 2121

- `discover_ps5()` — the LAN sweep that finds the console after an IP change scanned the configured
  FTP port only, so a console running etaHEN was findable **solely** by its DPI port, and invisible
  whenever the install host happened to be down. Now scans 2121, 1337 and the DPI port.
- `deploy.py` — `deploy.py app` is how the on-console UI gets updated. It connected to the
  configured port and gave up, so a console on etaHEN **could not be deployed to at all**; it
  printed "upload error" and the PS5 went on serving the old page. `ftp_login()` now walks the same
  fallback list and says which port answered.
- `doctor.py` — reported "FTP :2121 no route" on a perfectly healthy console. It now probes both and
  prints the live port, plus what config claims when they differ.

### The port a service is actually on is reported, not assumed

`ftp_port` is new in `/api/health` (both servers) and in the `/api/helpers` and `/api/sources` helper
blocks. `helper_status()` now passes the console's own `shadowmount_port` / `install_host_port`
through instead of overlaying config on top of them, and surfaces `etahen_ipc`. The UI prints those
values instead of the `:2121` and `:9021` constants it used to hardcode. Additive keys only.

### Verified live after the change

```
ftp_ok            -> True, sticky port 1337
_ftp()            -> 220 FTP server 1.0A for PS5 by LM.
helper_status()   -> ftp_port 1337 · shadowmount_port 10101 · etahen_ipc true · source "console"
dpi_host_kind()   -> etahen
```

### Install routing, re-confirmed end to end

Traced rather than trusted: **every install of every kind goes to etaHEN.** `dpi.mode="v2"` +
`dpi.host="auto"` → `dpi_host_kind()` sniffs `:12800` (it answers `<title>etaHEN DPIv2</title>`) →
`_install_etahen()` → `POST /upload`. The UI has exactly one install entry point,
`POST /api/install`. Packages already on the console take the console-local lane, and the ELF's
`dpi_install_url()` sniffs the same port and speaks the same multipart protocol to the same daemon.
`UNKNOWN` refuses rather than guessing Arsenal's protocol.

---

## [3.24.1] – [3.24.3] — 2026-08-23 · reconstructed `[VERIFIED]`

Three version bumps shipped without changelog entries. There is no VCS in this tree, so the split
between .1, .2 and .3 is not recoverable; this is the combined content, reconstructed by diffing the
`2026-08-23_2100` backup (3.23.0) against the tree and subtracting what 3.24.0 already documents.
All of it is in `ps5-app/onconsole/` — the Python and the UI changed only their version stamps.

- **ShadowMount is on 10101, not 9021.** It announces its own listener
  (`[API] HTTP/JSON ready: http://127.0.0.1:10101/api/v1`). 9021 is elfldr, always running. Because
  that number is *also* the "is it already up?" test in `payload_bundle.h`, the check was permanently
  true and **we never actually auto-started ShadowMount** — it only ever ran because the user
  autoloads it from Payload Manager. Corrected in `payload_bundle.h`, `/api/helpers` and the boot
  notification. (`/api/sources` and `/api/health` were missed — see 3.24.4.)
- **etaHEN's jailbreak IPC port is reported, never spoken to.** `/api/helpers` gained
  `etahen_ipc_open` / `etahen_ipc_port` (9028). Upstream documents it as localhost-only; on this
  build it answers from the LAN too. It is a channel that jailbreaks processes and its command set
  is unknown, so we report it and stop there.
- **`?bigapp=0` A/B probe on the in-process installer.** `install_by_package_inproc` unconditionally
  called `sceSystemServiceGetAppIdOfRunningBigApp()` first, on the strength of a comment claiming
  the working reference implementation does the same. Reading etaHEN's GPLv3 source shows it does
  **not**: `DirectPKGInstaller.cpp` never calls it; the symbol is used in its daemon/jailbreak code
  only. It is now a parameter (`install_by_package_inproc_ex`) because it is the leading suspect for
  why subtype=7 (add-on) BGFT tasks can be created but subtype=6 (game) ones cannot — announcing a
  running big app immediately before an APP install plausibly steers ShellCore into
  `applyPatchPrimarySlot`, the call that errors in the failing trace and that an add-on never
  touches. **Default unchanged (`announce_bigapp = 1`)**; only the probe passes 0.
- **Helper autostart waits for the port instead of trusting Payload Manager.** "started" used to
  mean only that Payload Manager *accepted* the request. It now polls the helper's port for up to
  12 s and reports `started` / `started-but-port-silent` / `launch-failed` / `write-failed`, so the
  status is observed rather than assumed — every downstream diagnosis had been resting on that claim.
- **`autostart_wait_until_ready()` runs once per boot.** It is called from both the tile path and the
  helper path, and without a guard the full user-configured delay was served *again* on the second
  call — a console with a missing tile waited it out twice before any helper was touched.

---

## [3.24.0] — 2026-08-23 · "The console does tell us, and PlayGo is the installer" `[VERIFIED]`

Two long-standing beliefs in this codebase were tested and both were wrong.

### The console DOES announce suspend — we were never listening

`server.c` asserted, in a comment, that *"a PS5 payload gets no suspend notification"*. **False.**
The firmware publishes power state into four **named kernel event flags** any process can open by
name, and the proof was already on this console: `shadowmountplus.elf`, **a payload we ship
ourselves**, imports `sceKernelOpenEventFlag`/`PollEventFlag` and logs
`[SHELLFLAG] monitor started (4/4 flags opened)`.

Added `power_watchdog()` — **observe only**. It opens `SceSystemStateMgrInfo`,
`SceSystemStateMgrStatus`, `SceLncUtilSystemStatus`, `SceShellCoreUtilAppFocus` and polls them
every 200 ms, logging timestamped deltas and state transitions to a ring buffer readable at
`GET /api/power`. Live on first boot: **4/4 opened**, `SceSystemStateMgrInfo = 0x00000003000003e8`
→ `STATE=WORKING(1000) trigger=0x0003` — byte-identical to ShadowMount's own reading.

It stops nothing and writes nothing, on purpose. Its only job is to measure the one number nobody
has: the **runway** between the first pre-suspend edge and power-cut. Acting on that edge before
the runway is known is a coin flip with a kernel panic on the losing side, and the manual button
already works.

Two rules are load-bearing and documented in the code: poll with **wait-mode OR and clear
nothing** (a clear-mode would consume power-transition bits out from under ShellUI mid-shutdown),
and **a negative return means the out-pattern was not written** (treating it as zero invents
transitions that never happened).

API shape taken from ShadowMountPlus (GPL-3.0, `src/sm_shellcore_flags.c`); this project is GPLv3.

### The install blocker is decoded — and we are calling the RIGHT API

`0x80B2116F` — unsolved since the engine manual was written — decodes via etaHEN's own embedded
error table to **`SCE_PLAYGO_ERROR_CORE_INVALID_SLOT`**. Not a credentials error. The manual's
§2.2 conclusion ("we are not blocked on privilege") was right; the search was just looking in the
wrong place.

Then we watched a **real install happen**, passively, by attaching to etaHEN's klog on `:9081`
while a DLC installed end to end, and captured what the system does internally:

```
scePlayGoCoreGetRawContentInfo(uri = http://.../X.pkg)  -> 0x00000000
[PlayGoCore][Uninstall]     (<content id>)              -> 0x00000000
[PlayGoCore][DbgInstall]    (<content id>)              -> 0x00000000
     BGFT Task ... [type=1,subtype=7,slot=00] : registered
[PlayGoCore][CreateRequest] (<content id>, 0)           -> 0x00000002
[PlayGoCore][RequestInstall](#2, <content id>)          -> 0x00000000
     prepare -> transfer -> install, state=7 error=0x0
```

**CORRECTED SAME DAY.** The first reading of this trace was that etaHEN drives PlayGoCore
directly and we call the wrong API. That is **wrong**. etaHEN's daemon imports exactly two
installer symbols — `sceAppInstUtilInstallByPackage` and `sceAppInstUtilInitialize` — and its own
GPLv3 source calls the first at `DirectPKGInstaller.cpp:1204`. The PlayGoCore chain above is the
**interior** of that one call, logged by the ShellCore side of the IPC. No PlayGoCore library even
exists on the console.

So we call the correct and only public entry point, and the structs we pass match etaHEN's byte for
byte — including the zeroed `PlayGoInfo`, which etaHEN also zeroes and still succeeds with.

**The discriminator is the caller's environment.** The process that succeeds runs at authid
`0x4800000000000006`; we use `0x4801000000000013` in-process or borrow `0x4800000000000010`. We
have never tried `...006`. That is the next experiment.

One thing that makes this safe to iterate on: `0x80B2116F` is a **pre-BGFT refusal** — no task is
registered, so a failed attempt cannot wedge the install lane, dirty `app.db`, or leave a
`bgft.db` row.

Full trace preserved at `research/klog-working-install-2026-08-23.txt`, analysed in
`research/install-call-chain.md`. Verified the observed install really landed: `bgft.db` 1026,
error 0x00000000, 1376256/1376256.

### Fixed — ShadowMount auto-start has never actually worked

The bundle entry used port **9021** both as ShadowMount's health probe and as its
"is it already running?" de-duplication test. **9021 is elfldr**, the ELF loader, which is always
running — so the check was permanently true and we never launched ShadowMount. It only ever ran
because the user autoloads it from Payload Manager.

ShadowMount announces its real listener in its own log:
`[API] HTTP/JSON ready: http://127.0.0.1:10101/api/v1 (v1)`. Now 10101 everywhere — the bundle
de-dup, `/api/helpers`, and the ready toast, which was reporting a green light for the wrong
process.

### Other

- **Helper start is verified, not assumed.** "started" used to mean only that Payload Manager
  accepted the request. It now waits up to ~12 s for the helper's port to answer and reports
  `started-but-port-silent` when it does not.
- **The autostart delay is served once per boot.** It was called from two paths with no guard, so
  a console whose tile is missing waited the full user delay twice before any helper was touched.
- **`/api/helpers` reports `etahen_ipc_open`.** etaHEN's jailbreak IPC on `:9028` is documented
  upstream as localhost-only; on this build it answers from the whole LAN (measured from another
  machine, with 9021 and 10101 refusing as controls). We report it and never speak to it — the
  command set is unknown and it is a channel that jailbreaks processes.

### Not done, deliberately

Acting on the pre-suspend edge (A2) waits for A1's measurement. And etaHEN's own mechanism — a raw
14-byte inline detour on a function inside SceShellUI — is **refused outright**: a second
uncoordinated patcher in the same process as etaHEN's live hooks, running while ShellUI tears down
for rest, is the highest-panic-risk idea surveyed, and it buys nothing the event flags do not.

---

## [3.23.0] — 2026-08-22 · "Stop believing the comments, measure the console" `[VERIFIED]`

A second measured pass: 8 parallel audits over the whole app, 115 findings, the top 8 put through
adversarial verification. Four survived, four were refuted — including two of the loudest.

### Fixed — a 2.09 s dead-port probe on card taps

`CheatRunner.alive()` had no cache. CheatRunner's `:9999` is not running, and a refused TCP connect
costs the full ~2.03 s on Windows. Every `/api/mods/<tid>` for a title with **no** local cheat file
paid it, and so did `/api/health` once every ten minutes when `running_title()`'s negative memo
expired.

Measured: `/api/mods/CUSA00050` **2090 ms → 33 ms**. A title that *does* have mods (`CUSA00503`)
still returns all 38.

The cache is keyed off the **result**, not an exception — `_call()` swallows every error and
returns `{"ok": False, "unreachable": True}`, so a `try/except` memo would never have armed. It is
a short symmetric TTL rather than a long negative memo, so someone who starts CheatRunner is picked
up within 15 s instead of being told it is offline for ten minutes.

### Fixed — "Fix install-port conflicts" stopped the two things it must not

`CONFLICT_PROCS` listed `nanodns.elf`, `ftpsrv.elf` and `klogsrv` as Elf Arsenal's leftovers. They
are not. `/data/pldmgr/autoload.txt` on this console reads
`!5000 / nanodns.elf / ftpsrv.elf / kstuff-lite_v1.10.elf / shadowmountplus.elf` — they are Payload
Manager autoload entries and come back every boot. **This repo already recorded that correction in
a previous changelog entry and it was never propagated to the code.**

On a healthy console those two were the *only* matches, so the button stopped exactly what cannot
contend for `:12800` and nothing that can. `ftpsrv.elf` owns `:2121` — the FTP this app reads
`app.db` and `bgft.db` over and deploys the ELF through.

The same tuple also answered "is Arsenal running?", so `_arsenal_running()` returned **True** on an
Arsenal-free console. Every install then ran the full Arsenal preflight: an FTP fetch plus two
refused `:6969` connects, **~4.1 s of dead time per install**. Verified: old tuple matched
`['ftpsrv.elf','nanodns.elf']` → True; new tuple matches nothing → False, and the sweep is now a
genuine no-op with both payloads left running.

### Fixed — the rest-mode text described a console that no longer exists

The behaviour is **unchanged and deliberately so** (see below). Three strings were wrong: the
home-page tooltip and the `confirm()` dialog both named Elf Arsenal and GarlicSaves — neither of
which runs here — and both **omitted etaHEN, which the sweep does stop**, taking the install lane
and FTP with it. The Settings tooltip was already correct and was left alone.

### Why etaHEN stays in `REST_STOP`

etaHEN ships its own rest-mode switches — `Util_rest_kill`, `Game_rest_kill`,
`Rest_Mode_Delay_Seconds`, `disable_toolbox_auto_start_for_rest_mode` in `/data/etaHEN/config.ini`
— and self-heals its daemons. That proves upstream treats killing them across a suspend as a
*supported* mode. **It does not prove leaving them alive is safe**, and nobody has tested that on
12.70.

The asymmetry decides it: being wrong costs a kernel panic on suspend, the exact failure that
already cost a console rebuild. Being conservative costs one reload from Payload Manager — which
the user already performs every boot, because etaHEN ships with port 0 and is never auto-started.
The controlled experiment that would settle it (`/api/rest/prepare?self=0` with `"etahen"`
temporarily removed, then rest and wake) is recorded in the code comment.

### Performance

- **Drawer banner two-staged.** `.dbanner` is 160 px tall and was loading the 512×512 original —
  461–537 KB, ~1 MB of decoded RGBA — synchronously on every card tap. It now paints from the
  thumbnail already decoded in the card the user just tapped, and swaps in the original only once
  it has decoded. The `<img>` is reused instead of rebuilt, so reopening a title re-decodes nothing.
- **Queue rows no longer rebuilt while the drawer is shut.** `renderQueue()` runs every 1.2 s
  during an install and rebuilt every row each time, visible or not. The pill, ring and count still
  update; `dockShow()` paints the rows on open.
- **`/api/health` probes memoised for 5 s.** Every poll opened fresh TCP connections to the console
  for `ping()` and `ftp_ok()`, every 6 s, from every client — landing on the console's
  single-threaded accept loop while it serves the page being scrolled. Memoised in the *handler*,
  not in `ping()`/`ftp_ok()`, so the install preflight still gets a live answer.
- **`:6969` pokes gated on the port being open** — 2.03 s each, result discarded, on a console with
  no Arsenal.
- **`/api/network/scan`** no longer blocks on a 254-thread sweep; bounded to 2.5 s.

### Security

- **Origin guard.** Both servers answer `Access-Control-Allow-Origin: *` with no auth — fine for
  someone already on the LAN, where Payload Manager on `:8084` will load an arbitrary
  kernel-privileged ELF for anyone who asks. What is *not* fine is a page on the public internet:
  any site the user visits, in any browser on the network, could fire requests at the companion.
  The discriminator is not "same origin" — the console legitimately calls this PC cross-origin —
  it is whether the origin is a **private-range IP literal at all**. Verified: `https://evil.com`
  → **403**, `http://10.0.0.99:8710` → 200, no-Origin (curl) → 200. Applied to every POST and to
  `GET /api/rest/prepare`, which a hidden `<img>` could otherwise have fired.
- **`esc()` now escapes single quotes.** It is used inside single-quoted attributes; a game name or
  peer string containing `'` could break out.
- **Peer-supplied art URLs validated** before reaching an `img src` — `icon_url`/`thumb_url` come
  from another machine over the LAN. Anything that is not a plain http(s) URL falls back to our own
  `/icon/` path.

### Refuted — measured and deliberately NOT done

- **`/api/mem/write` as "critical".** The premise was that it is the primitive the cheat engine is
  built on. It is not: repo-wide there are **zero callers** — the engine reaches `mem_write()`
  in-process. Gating it while `:8084` loads arbitrary kernel payloads reduces attacker capability
  by zero.
- **`/api/engine/install-url` arbitrary write.** Its `dest` parameter has **no producer anywhere in
  the shipped system**, and the basename is already sanitised. Moving it to POST would break the
  install lane for every companion not updated in lockstep — including the 3.21.3 host on the LAN.
- **Removing `"etahen"` from `REST_STOP`** — see above.
- **The 13-endpoint unauthenticated-GET wall** — the right shape *if* ever done, but one change
  touching thirteen live paths on a daily-driver console, for a threat model `:8084` already blows
  open.

---

## [3.22.1] — 2026-08-22 · "The console was never using the thumbnails" `[VERIFIED]`

A measured performance pass on the PS5 browser: eight parallel audits, six findings put through
adversarial verification, five refuted. The headline is not a clever optimisation — it is that the
optimisation shipped in 3.21.0 never reached the console at all.

### The thing that mattered — covers were still full-size PNGs

**The PS5 does not use the console's API.** The page is served by the console, then `resolveApi()`
reads `on_console:true`, calls `/api/companion`, and re-points `API` at a **PC companion**. From
that moment every `/api/*` call and every cover comes from a PC over the LAN.

The console's own `/api/library` sets `thumb_url` on 112 of 113 titles. The PC companion's
`/api/library` set it on **0 of 113** — `build_library()` never added the field, and the peer-merge
branch copied `icon_url` while dropping the `thumb_url` the peer advertised. `iconUrl()` prefers
`thumb_url`, found none, fell past `icon_url`, and landed on the full-size PNG. Both halves looked
correct in isolation, which is why this survived two releases.

Measured over a 20-title sample on the live host:

| | per cover | whole library |
|---|---|---|
| before (PNG) | 370,335 B | 39.2 MB |
| after (WebP) | 19,973 B | 2.11 MB |
| | **18.5x** | |

Decoded bitmap held in the DOM for a ~50-card window drops from ~50 MB (512x512x4) to ~19.5 MB
(320x320x4). That is the memory pressure that made covers evict and re-decode while scrolling.

`iconUrl()` now also **derives** `/thumb/<TID>.webp` from the API host when a companion is too old
to advertise it — verified live against a 3.21.1 peer, which served a 7,466-byte WebP for a title
whose `/api/library` carried no `thumb_url`. Never against the console, which has no `/thumb` route
(verified: 404).

### Covers step down instead of vanishing

`onerror="this.remove()"` meant one miss — no WebP support, no `/thumb` route, a thumbnail that
failed to generate — silently deleted the artwork and left a letter. Now: thumbnail → full-size
original → initials.

### WebP is detected, not assumed

WebP arrived in Safari 14 / WebKit 610. Nothing observable from outside the console proves the PS5
browser is newer, and being wrong would blank every cover. A 68-byte 2x2 WebP is decoded at boot and
`iconUrl()` skips `thumb_url` entirely if it fails, repainting the grid once. The probe image was
round-trip tested before shipping.

### Scrolling inside a panel ran the grid virtualizer

`virtOnScroll` is bound to the **document** in the capture phase, because scroll does not bubble —
so it also fired for scrolling inside the detail panel, and `virtRender()` then read `scrollTop`,
`offsetTop` and `clientHeight`, forcing a synchronous layout every frame of a scroll that cannot
have moved the grid. It now returns immediately while an overlay is open, and resyncs on close.

### Other measured fixes

- **`/api/sources` took 760 ms** to return 564 bytes: it probed ShadowMount's port 9021 from the
  LAN, and that port binds **loopback-only**, so the probe could only ever burn its full timeout —
  and the on-console answer overwrote the value anyway. The two probes that can succeed now run in
  parallel. Measured **760 ms → 12-28 ms**, on the boot path.
- **JSON responses gzip** when the client asks: `/api/library` **186,878 → 21,342 B** (8.8x). This
  is on the real LAN wire; the page shell is not, so the console C server is deliberately left
  alone (and `Content-Encoding` would collide with the install lane's 206/Range responses).
- **Cover art now carries a validator.** `/icon` was `max-age=86400` with no ETag, so every drawer
  icon was re-fetched in full daily. Both `/icon` and `/thumb` are now
  `public, max-age=604800, immutable` with an ETag; revalidation returns **304, 0 bytes**.
- **A 40px-blur `box-shadow` was animated on card hover** — repainting a region larger than the card
  once per card as the cursor sweeps a row. Removed from the transition; it snaps.
- **`content-visibility:auto` was still on the cover `<img>`**, eleven lines under the comment
  explaining why it had been removed from `.card` for making covers flash. Gone.
- **Every grid render built the window twice.** The 10px scrollbar made the probe card measure
  ~2px taller than the settled grid, so `VIRT.remeasure` fired and rebuilt everything one frame
  later. Fixed at the cause with `scrollbar-gutter:stable` + `overflow-y:scroll`, not with a
  threshold — the delta was measured at 1.81px at 1920 but 2.14px at 1280, so any threshold would
  have let the bug survive at the smaller size.
- **Four intervals ran forever**, whether or not anyone was looking. They now pause on
  `visibilitychange` and re-arm with an immediate refresh; storage polling drops 15s → 60s on the
  console. The queue poll's cadence is untouched and still only fires while a task is live.
- **The drawer banner** decoded a 512x512 PNG synchronously on every card tap; now `decoding="async"`.

### Automatic device profile

`PROFILE` uses three separate signals, each only for what it can prove: `onConsole` (a server fact
from `/api/health`, captured before `resolveApi` clears it), `ps5` (UA — cosmetic gating only), and
`webp` (a real decode test). On the console, `backdrop-filter` is dropped from the two drawer
buttons — they sit over the banner while the drawer runs a transform+opacity transition, so the blur
was recomposited every frame of every open and close.

### Fixed — the console could not rank companions

A companion's version arrived **only** inside a federation document read during a library merge, so
a warm peer cache meant it never arrived. `/api/pcs` reported every PC as version `""`, `ver_cmp()`
had nothing to compare, and selection fell back to "most recently seen" — which is how the console
picked an *older* companion right after a restart. The PC now states its version on every
registration. Verified: `{'10.0.0.76': '3.22.1'}` → chosen.

### Deliberately NOT done (each was measured and rejected)

- **gzip on the console C server.** The PS5 loads the shell over **loopback** — compression saves
  nothing there, and `Content-Encoding` is incompatible with the `206`/`Accept-Ranges` responses the
  install lane depends on.
- **Lowering `VIRT.pad`.** Measured: pad 4→1 moved steady-scroll median only 2.8 → 2.3 ms while
  cutting the recycle overlap budget from 4339 px to 1972 px against a 6410 px range — dropping
  routine flicks into the full-teardown path, which is the visible cover-reloading this release is
  trying to eliminate.
- **Threading the console's accept loop, or keep-alive on it.** The loop is an implicit lock across
  every API handler, and `sceAppInstUtilInitialize` must run exactly once. A persistent connection
  pins it — that failure already took the whole API down once, which is why the 8s `SO_RCVTIMEO`
  exists.
- **Pointing `iconUrl()`'s final fallback at `/thumb`.** The console has no such route (verified
  404); it would blank every cover whenever no PC is reachable.

---

## [3.21.3] — 2026-08-22 · "A PS5 DLC is a PKG, not a backup" `[VERIFIED]`

Grouping PS5 add-ons onto their game's card (3.21.2) exposed two bugs that had never been reachable
before, because before it no PS5 add-on was ever offered for install.

### Fixed — an add-on inherited its game's container format and was routed as a backup

`is_backup_item(item, game)` falls back to **the game's** format when the item does not state one.
That is right for the base item and wrong for everything else. A PS5 game arrives as a `.ffpfsc`
ShadowMount container, so the moment its updates and DLC were grouped onto that card, every one of
them inherited `ffpfsc` from the parent and was declared a backup.

They were then sent down the mount/peer-delegate lane, which files a task as `kind:"backup"` — and
that cost them their real kind. The install watcher saw `kind:"base"`, took the base-game branch,
and waited for a full-size `app.pkg` that a 1 MB DLC can never produce. After the 240-second
timeout it reported:

> Console rejected this package — delivered in full, but it won't install (bad dump)

**The install had actually succeeded, completely.** Measured on Borderlands 4's *Firehawk's Finery*:
`bgft.db` status `1026`, `error_code 0x00000000`, `1114112/1114112` bytes, and `addcont.db` holding
the registration under its real name. Only the report was wrong — the user would have concluded PS5
DLC installs were broken and reinstalled them repeatedly.

An add-on is a PKG. Always. `is_backup_item()` now says so before consulting the parent game.
Verified against the old implementation side by side: **exactly two verdicts change** (PS5 DLC and
PS5 update), and PS4 base/update/DLC and every backup container are bit-for-bit unchanged.

After: lane `install`, source `peer`, **`playable` in 15 seconds** instead of four minutes to a
false failure. Confirmed on device with a second, freshly installed DLC (*Ornate Order Pack*,
`1026`, `1245184/1245184`, registered).

### Fixed — every PS5 package was reported as corrupt

`pkg_completeness()` tested only PS4's fPKG magic `7F 43 4E 54` (`CNT`). A PS5 package is a
different container with magic `7F 46 49 48` (`FIH`), so all of them came back
`{"complete": false, "confident": true, "reason": "bad PKG magic (not a .pkg)"}`.

`confident: true` is the level that is allowed to **block** an install. It did no harm only because
`integrity.on_local_corrupt` defaults to `"warn"` — set it to `"block"`, which the setting exists to
allow, and not one PS5 add-on would install. The verdict also decorated the false error above.

PS5 packages now return **inconclusive** (`complete: true, confident: false`), which is the honest
answer: the PS4 offsets this function reads mean nothing in that container, so it must not pretend to
judge it. Verified on a real file of each type — PS5 inconclusive, PS4 still fully validated.

---

## [3.21.2] — 2026-08-22 · "The peer add-on table was silently full" `[VERIFIED]`

PS5 games showed one card each (3.21.1's companion fix worked), but on the console **not one of the
54 PS5 cards carried a single update or DLC**, while PS4 cards showed theirs normally.

### Fixed — `PEER_ADDON_MAX` was a hard 32 and dropped the rest without a word

Add-ons a PC holds for a game the console already has are parked in `g_peer_addons[]` and spliced
into the card after the peer block is built. That array was a fixed 32 entries, and the write site
simply skipped anything past it — no log, no error, no truncation marker.

Peers are merged one after another. The first peer alone held **46** PS4 titles carrying a patch or
DLC. It filled the table, and every title merged after it got nothing. The PC holding the PS5 games
is merged second, so *all four* PS5 titles with add-ons landed past the end. Measured: **50 slots
needed, 32 available, 18 dropped.**

It reads as a PS5 grouping bug and is not one — it is a capacity limit that happens to fall on
whatever is merged last.

The table now grows on demand. Nothing is allocated until a peer actually has add-ons, and a
`PEER_ADDON_HARD_MAX` of 1024 exists only so a broken peer cannot make the console allocate without
bound.

After, on the console: PS5 **0 → 1 update and 4 DLC**; PS4 **33 → 44** cards with updates and
**12 → 13** with DLC — those PS4 add-ons had been dropped too. Card counts unchanged (59 PS4 / 54
PS5), zero duplicates, library JSON still valid.

---

## [3.21.1] — 2026-08-22 · "Scrolling stops throwing the library away" `[VERIFIED]`

3.21.0 made console scrolling **worse** — covers visibly reloaded on the smallest scroll. That was a
regression introduced here, and it came from fixing the wrong layer.

### Fixed — the grid was rebuilt from scratch on every row crossed

`virtRender()` did `grid.innerHTML=""` and rebuilt the whole window each time the visible range
changed. Every card *and every `<img>` inside it* was destroyed and recreated, so a nudge of the
scroll threw away 24-112 images and asked for them all again — cached or not, a fresh `<img>` still
costs a fetch check, a decode and a paint.

3.21.0 then cut `VIRT.pad` from 6 rows to 2. That reduced how many cards were built, but it also made
the window *tighter*, so the full rebuild fired far more often. Fewer cards, rebuilt constantly —
worse, not better.

The window shifts contiguously while scrolling, so the overlap between old and new is nearly total.
`virtRender()` now **trims the ends and splices in only what is genuinely new**; existing cards, and
the decoded images inside them, are never touched. A jump with no overlap (search, filter, a fling)
still falls back to a clean rebuild. `pad` is back to 4 now that a shift is cheap.

Measured on the console, scrolling two rows: **36 of 36 images survived, 12 new** — previously all
36 were destroyed and recreated.

`content-visibility:auto` was also removed from cards. It skips rendering offscreen and then has to
paint from nothing on entry, which reads as flashing. With recycling, offscreen cards simply are not
in the DOM — strictly better than asking the browser to skip them.

### Added — card covers are thumbnails, not 512x512 originals

The source art is 512x512 PNG averaging ~280 KB, which every browser decodes to a **full 1.0 MB of
RGBA** — for a tile drawn about 220 px wide. Thirty visible cards meant thirty megabytes of decoded
bitmap. That, not rendering, is what the console was struggling with.

The companion now generates a 320 px WebP once per title (`/thumb/<TID>.webp`, cached in
`.cache/thumbs`, Pillow was already bundled) and advertises `thumb_url` over federation. Measured
across six real covers: **1,929,928 -> 105,166 bytes, 18.4x smaller**, ~0.04 s to generate each,
generated once and then served from cache. Cards use the thumbnail; the detail panel still gets the
full-resolution original.

The console has no image library, so it cannot resize its own art. It now points `thumb_url` at the
best companion (`best_pc_base()`, ranked newest-build-first exactly like `/api/companion`), which
generates the thumbnail on demand — pulling the icon off the console over FTP first if it has never
seen that title. Verified: 112 of 113 console titles now carry a thumbnail URL.

### Fixed — a blanket cache header froze the UI at the old version

3.21.0 gave every response `max-age=604800`. That included `index.html`, so loading a new ELF left
the console running the **old UI for a week** — the browser never asked again. Caught immediately:
the console kept reporting `pad:2` and full-size icons after a build that had neither.

`send_file()` now picks by content type: images and fonts keep
`public, max-age=604800, immutable`; everything else gets `no-cache, must-revalidate`. The ETag means
an unchanged shell still costs only a 304. Verified live — `index.html` revalidates,
`assets/logo.png` and `/icon/*.png` stay immutable.

## [3.21.0] — 2026-08-22 · "PS5 games get one card, and the console stops rendering the whole library" `[VERIFIED]`

### Fixed — every PS5 update and DLC had its own library card

A PS5 game showed up several times: the real game, plus extra cards named after content ids like
`UP1001- 00-FIREHAWKSFINERY0`. Across the whole PS5 set: **0 entries had updates, 0 had dlc**, while
PS4 had 44 and 15. Two independent causes, found by reading bytes rather than guessing:

**1. `parse_pkg()` returns `None` for every PS5 package.** A PS4 fPKG starts with `7F 43 4E 54`
(`CNT`); a PS5 package starts with **`7F 46 49 48`** (`FIH`) — a different container. The
entry-table reader bails at the magic check, so there is no `param.sfo`, no `CATEGORY`, no `TITLE`
and no version. (Its metadata is not recoverable another way either: the SFO magic appears nowhere
in the first 4 MB of four different PS5 packages.) With no CATEGORY the item fell to
`filename_fallback()`, which hard-defaults to `kind="base"` and names the card after the content id.

`filename_fallback()` now classifies PPSA titles from the filename, which is deterministic:
a version triple (`-v01-000-000`) means an **update**; anything else is **dlc**, because a PS5 game
arrives as a ShadowMount `.ffpfsc` backup and never as a `.pkg`. **This function is only ever
reached when `parse_pkg` fails**, which for PS4 essentially never happens — and the new branch is
gated on `PPSA`, so a CUSA title cannot enter it.

**2. The game and its add-ons were built into two different lists.** `.pkg` items are grouped by
title into `by_title` (`lane:"install"`); `.ffpfsc` backups are appended separately from `mounts`
(`lane:"mount"`). A PS5 game with a backup *and* add-on packages therefore produced **two cards for
one title id**. They now merge: the backup becomes the card's base, the packages stay as the
updates/DLC they now classify as, and the card keeps the real game name. Duplicate identical
containers (the same backup reaching us twice) are dropped.

Verified before changing anything that **no PS4 title has this split** — 4 titles were affected, all
PS5 — so the merge cannot move the PS4 path. Confirmed after: PS4 is byte-identical at 59 entries,
base 56, updates 44, dlc 15, update_only 3, zero duplicate cards.

End-to-end on a synthetic library mirroring the real filenames — **4 games, 4 cards**:

    PPSA01494  Borderlands4              base=backup  + 3 dlc
    PPSA07064  Lords of the Fallen       base=backup  + 1 dlc
    PPSA15656  Disney Epic Mickey        base=backup  + 1 update + 1 dlc
    CUSA14409  Castle Crashers (PS4)     base + update, lane=install     <- unchanged

### Fixed — the console rendered far more of the library than it showed

Scrolling lagged on the PS5 while the PC was fine. Three multipliers stacked:

- **`VIRT.pad` is measured in ROWS**, so the overscan multiplied by the column count: 6 rows × 7
  columns is ~112 cards built for ~21 visible. Now 2 rows — measured on the console: **24 cards per
  window instead of 48 here, and ~5× fewer on a TV grid**.
- **Four `backdrop-filter: blur()` regions per card** (`.plat`, `.badge`, `.tid`, `.sz`) — roughly
  450 blur regions per window, the most expensive single property on this GPU. Removed from cards
  (backgrounds darkened slightly so they stay readable); panels and buttons keep theirs, there are
  only a handful on screen. Measured after: **0 backdrop-filter regions on cards**.
- **Covers were eager and uncached.** Every card image is now `loading="lazy"` and
  `decoding="async"`, and cards carry `content-visibility:auto` + `contain`, so an offscreen card
  costs no layout or paint.

### Fixed — the console re-downloaded every cover on every rebuild

`send_file()` sent **no `Cache-Control`, no `ETag`, no `Last-Modified`**, so a browser had no
validator and reused nothing — each grid rebuild refetched every ~360 KB cover through an accept
loop that is **single-threaded** and shared with the install engine and the cheat engine. It now
sends `Cache-Control: public, max-age=604800` and a size+mtime `ETag`. Verified live on
`/assets/logo.png` and `/icon/<TID>.png`.

`build_library_json()` also hardcoded `"has_icon":true` for **every** app.db title without ever
stat()ing the file. The UI only emits an `<img>` when that is set, so any title without art fired a
request that 404s — one per card, through the same serial loop. It now calls `icon_exists()`.

### Note — Casita must run 3.21.0 for the PS5 grouping to appear on the console

All the PS5 games and their add-ons live on the second PC. The console renders whichever companion's
library it is given, so the grouping fix has to run **there**. The console prefers the newest
companion (v3.20.3, I30), so once Casita is updated it takes effect immediately.

## [3.20.3] — 2026-08-22 · "The console was driving an old PC, and the DPI never got cleaned" `[VERIFIED]`

Three separate faults behind *"second install says dpi wedged"*, *"it activated Elf Arsenal again"*
and *"the version shows 1.1.1, then 3.19.0, then 3.20.2"*. They share one root: **the console was
using the wrong companion.**

### Fixed — the console picked its companion by luck, and could pick an OLD build

`/api/companion` sorted the registered PCs by `last_ms` — most recently seen. Every companion
re-announces every 8 s, so which one the console used was effectively a coin flip. With two PCs on
the LAN it was picking **Casita on 3.19.0** over this PC on 3.20.2:

    {"urls":["http://10.0.0.72:8710","http://10.0.0.76:8710"],"url":"http://10.0.0.72:8710"}

That is not cosmetic. Driving an old companion means driving an old **engine** — and 3.19.0 still
has the Elf-Arsenal-shaped readiness probe (so every install reads **"dpi wedged"**) and the
hardcoded `payload_path("arsenal")` recovery (which **relaunches Elf Arsenal**). Pressing Install on
the PS5 therefore resurrected Arsenal and restarted the :12800 port war. Every symptom reported,
from one cause.

Companions are now ranked by **version first** (`ver_cmp()`, numeric per component — a string
compare puts "19" after "2"), then by recency. The version is captured from each peer's
`/api/federation` into `pcpeer_t.ver`, and `/api/pcs` now reports `name` and `version` so it is
visible rather than guessable. Verified live: with Casita on 3.19.0 and this PC on 3.20.3, the
console now returns `"url":"http://10.0.0.76:8710"`.

**A second PC still needed updating by hand at this point** — there was no self-update path yet. 3.86.0 added one.
Until then the ranking keeps it out of the way.

### Added — post-install DPI cleanup, the etaHEN equivalent of the Arsenal mechanism

Elf Arsenal had no cleanup endpoint; the mechanism there is `_dirty_hosts`, which makes the *next*
install reload the daemon first (I22). etaHEN cannot be reloaded — its DPI lives inside the daemon
that also serves FTP and klog — but it exposes **`POST /cleartmp`**, which drops what the transfer
left behind (`SUCCESS: Deleted N temporary files.`).

`Ps5Bridge.dpi_cleanup()` now runs at **both** job-completion sites, success or failure, so a queued
run starts each job on a clean daemon instead of inheriting the last one's leftovers. Because it
leaves the host genuinely clean, the dirty mark is cleared rather than forcing a pointless reload
attempt before every subsequent install.

### Fixed — the console-local lane still spoke Arsenal's protocol

`dpi_install_url()` in the ELF POSTed Arsenal JSON to `127.0.0.1:12800` unconditionally. Against
etaHEN that endpoint does not exist, so every console-local (USB / already-on-console) install died
with **"no reply from the install daemon"** — reproduced on hardware. It now sniffs the host the
same way the companion does and speaks multipart `/upload` to etaHEN. Its success test was also a
bare `strstr(reply,"ok")`, which matched the word "ok" anywhere in an *error* body; it now requires
`SUCCESS` (etaHEN) or a real `res:0` (Arsenal).

After the fix the same install returns the console's genuine verdict instead of a transport lie:
`SCE_NP_DRM_CONTENT_ERROR_UNSUPPORTED (0x80F00003)` — correct, because that title is a ShadowMount
`.ffpfsc` container and etaHEN's DPI installs `.pkg` only.

### Fixed — the version shown was three different numbers

`v1.1.1` was a hardcoded literal in the markup, only corrected once `/api/health` answered — and
that answer came from whichever companion was picked, which could be an old one. So the console
showed 1.1.1, then 3.19.0, then the real version. The tag is now painted from the build's own
`APP_VERSION` on `DOMContentLoaded`, and health can only *raise* it, never replace it with an older
companion's number (`verNewer()`).

### Verified on hardware

- **Three consecutive install → uninstall → install cycles of the same title: all `playable`.**
- **A queue of four installs: all `playable`**, with four clean bgft rows landing inside 45 s —
  `CUSA02365 107806720`, `CUSA14409 227540992` ×2, `CUSA11740 532217856`, every one `status 1036`.
- Install host stayed `ready=True live=live` throughout; Elf Arsenal never started.
- Console UI: one version everywhere (`verTag`, `APP_VERSION` and health all 3.20.3), 113 games,
  48 cards.

## [3.20.2] — 2026-08-22 · "Nothing happened when you pressed Install — because nothing was there" `[VERIFIED]`

Reported as *"pressed install and nothing happened — not downloading, not installing"*, with the
conclusion that the etaHEN engine could not work and we should revert to Elf Arsenal.

**The engine was never the problem.** Two separate UI/serving bugs made the library invisible, so
there was no card to press. Both are fixed; no engine code changed. etaHEN's capability was proven
before and after by sha256.

### Fixed — the grid virtualiser could render zero cards, silently

`renderGrid()` measures the card grid using a **single probe card**, and that measurement is
unreliable: a lone card in an `auto-fill` grid makes `getComputedStyle` report **one column**, and a
card whose cover has not sized yet measures ~46 px. Observed live: `VIRT.cols = 1`,
`VIRT.cardH = 60` against a real grid of 6 columns × 325 px.

Those numbers drive the visible window, and they drove it negative — `last = -6`. Then:

- `renderGrid()` clears `innerHTML` before calling `virtRender()`;
- `virtRender()` computed an empty window and **cached it** in `VIRT.first/last`;
- its memo, `if(first===VIRT.first&&last===VIRT.last) return;`, then short-circuited every later
  call — while the grid was empty.

Result: a grid with correct padding (6840 px of it) and **zero cards**. No exception, no
"no titles match your filters" banner, `state.games = 112`. The library simply was not there, and
neither was any Install button.

Three defects, three fixes: an empty window while games exist now falls back to a first screenful;
the memo will not short-circuit while the grid is empty; and the metrics are re-measured from a
**real** card on the next animation frame, re-rendering if they changed. Verified: 48 cards,
`cols=6 cardH=325`, correct through scrolling to the bottom and back.

### Fixed — the console's `/api/library` emitted invalid JSON

Separately, the on-console UI showed an empty library because `GET /api/library` returned a **parse
error at byte 12987**:

    "peer_url":"http://10.0.0.76:8710/li],"cheats":[]}

`merge_pc_library()` accumulated each add-on into a fixed `char bkt[3][2200]` behind a 600-byte
headroom guard — but one item can exceed 900 bytes (two 400-char escaped install keys plus the
URL), so it passed the guard and `snprintf` truncated **mid-value**. Worse, `bn[bi]++` ran even when
nothing had been written, so the comma logic still counted the item.

Items are now built whole and appended only if they fit entirely; `bn[bi]` counts only what was
really written; the bucket is 8192 (Jump Force alone has 21 DLC). `peer_addon_t.upd/.dlc` had the
same hazard — they are spliced verbatim into the document, so a truncated copy breaks the JSON for
*every* title — raised to 4096 and the copy now refuses to truncate rather than corrupt.

The main record append was already correct: it drops a whole title rather than write a partial one.

### Verified end to end, from the UI, on hardware

Clean console, etaHEN alone on :12800, no Elf Arsenal:

- Clicked the real **Install base** button in the drawer → `"Sent ✓ · in queue"`, queue badge 1,
  pill showing the game name. POST body carried
  `drive/mode/console/version/content_id/size/force` exactly as the UI builds it.
- `queued → submitting → transferring → promoting → playable`.
- **sha256 of the console's `app.pkg` vs the PC source: `84871d37…`, identical.** bgft
  `1036  227540992/227540992  err 0x00000000`, fresh timestamp; app.db registered; integrity `ok`.
- Console UI after the JSON fix: **110 games, 48 cards**, grid healthy, no horizontal overflow.
- Per-second process monitoring across the whole install: **not one process started or stopped.**
  Elf Arsenal never came back.

### Answering the question directly

No, we do not need to go back to 3.19.0, and etaHEN does have every capability required: it accepts
a URL, downloads it itself, installs through Sony's BGFT, and produces a byte-perfect result — the
hash proves it. What failed was our UI, twice, in ways that looked exactly like an engine failure.

## [3.20.1] — 2026-08-22 · "We were starting a kernel daemon nobody asked for" `[VERIFIED]`

A PS4 title installed under 3.20.0 (Castle Crashers) passed **every** check we have and then
crashed the game *and the console* on launch. A full audit of both lanes followed, then three
forensic measurements on the recovered console.

**The forensics eliminated two of the three candidates outright.** `bgft.db` held a single row for
`CUSA14409` at `2026-08-22T03:30:50Z`, `status 1036`, `227540992/227540992`, `err 0x00000000` — so
the install was genuinely real and complete (not our gate reading the previous install's files), and
there was exactly one submission (no duplicate job). What remained was the environment.

**And the "install does not work / it started Elf Arsenal again" report is now fully explained and
reproduced.** On 3.20.0, pressing Install ran this chain:

    dpi_install_ready()  POSTs a junk URL to /api/install  ->  etaHEN has no such endpoint
      -> probe always False  ->  "⏸ dpi wedged"
      -> recovery runs  ->  pm.payload_path("arsenal")  ->  WE LAUNCH ELF ARSENAL
      -> garlic / dpi.elf / tile-autoinst / klogsrv return, dpiv2 fights etaHEN for :12800

Both links are broken in 3.20.1, and it was verified live: with per-second process monitoring, a
full base-game install and a UI-shaped DLC install each completed without a single process starting
or stopping on the console.

### Fixed — 3.20.0 auto-started etaHEN, changing the console's jailbreak layer

3.20.0 gave etaHEN `port 12800` in the payload bundle, so our ELF launched it on every boot whenever
the port was free. **This console's own boot chain does not run etaHEN** — the user's
`autoload.txt` is `pldmgr → kstuff_lite → shadowmountplus`, with `etaHEN-2.6B.bin` present in the
folder and pointedly unlisted. etaHEN patches the kernel and ShellCore, so we were putting a second
kernel-touching daemon next to kstuff_lite that had never been part of a working boot — and the
first fake-signed PS4 title launched afterwards panicked the console.

A wrong byte in `app.pkg` makes a *game* crash. Panicking the whole console on launch of a
fake-signed title points at the fpkg/fself patch layer, which is exactly what changed.

**etaHEN is back to `port 0` — carried, never auto-started.** It still ships inside the ELF and is
written to `/data/pkg-mutant-shop/payloads/` every boot so the file is always the build we tested
against, but starting it is the user's decision. Changing the jailbreak layer must never be a side
effect of installing our app.

### Fixed — `HOST_UNKNOWN` silently meant "Elf Arsenal"

`Ps5Bridge.install()` fell through to Arsenal's JSON protocol whenever the host could not be
identified — and `dpi_host_kind()` returns UNKNOWN *precisely* when two hosts are fighting over
:12800 or one is restarting, which was the incident state. Sending Arsenal's request to etaHEN
resets the connection, and the substring classifier then read "reset" as *wedged* and re-POSTed.

Now the probe is re-run with `ttl=0` immediately before dispatch (the cached answer is up to 20 s
stale and the port changes hands inside that window), and UNKNOWN **refuses**: nothing was queued,
so the task is held with `dpi_host_unknown` and a message naming the fix.

### Fixed — four more Arsenal assumptions that survived the switch

- **`recover_dpi()` reported success having reloaded nothing.** With etaHEN it cannot reload
  anything and just confirms the host answers — but callers treated that as "healed", cleared the
  `_dirty_hosts` mark (making I22 a set that fills and empties with no effect) and re-POSTed on a
  false premise. The dirty mark is now only cleared on a genuine reload.
- **`last_install_error()` still read Arsenal's `last-install.json`** with no host check and no
  freshness filter — the same trap `last_install_via()` was already fixed for. It could report an
  hours-old Arsenal error as the verdict on an etaHEN install.
- **`ensure_install_host_safe()` (I21) was keyed on port ownership, not on Arsenal being alive.**
  It returned "ok" the moment etaHEN answered :12800 — on the reasoning that Arsenal "is not even
  running", which is exactly what was false during the incident. It now checks the process list.
- **etaHEN's `queued: false` was set and never read.** etaHEN validates a URL *before* queueing, so
  a rejection provably created no bgft job — the one case where a clean re-POST is safe, and the
  duplicate-job hazard behind invariant I4 does not apply. One retry is now taken.

### Fixed — a stale full-size install could satisfy the base-game gate

The base gate is "title in `app.db` **and** `app.pkg` ≥98%". For a title that was *already*
registered with a full-size `app.pkg`, both halves can be satisfied by the **previous** install's
files — `_bgft_before` was snapshotted for every job but only compared on the add-on branch. When
`was_registered` is true the gate now also demands a `bgft` row that differs from that snapshot and
is terminal. A genuinely new title needs no such proof: `app.pkg` appearing at full size where there
was nothing *is* the new evidence.

### Verified on hardware, 3.20.1, clean console

Console: etaHEN alone on :12800 (user-started), kstuff, ShadowMount, pldmgr, elfldr. Zero Arsenal.

- **Castle Crashers, 217 MB, base game** — `queued → submitting → transferring → promoting →
  playable` in 17 s. `app.pkg` mtime fresh, all five files, `bgft 1036 … err 0x00000000` timestamped
  to the minute, and — for the first time — **sha256 of the console's `app.pkg` hashed against the
  PC source: `84871d37f59a33ad6a67a4580342011f798b92a8d4c4c8c8bb255ae4dea8964f`, identical.** That
  is the only check that proves the bytes, and it is now part of the routine.
- **A DLC through the exact UI request shape** (drive/mode/console/version/content_id/force) —
  `addcont.db` 6 → 7.
- **PS5 mount lane untouched**: 34/34 ShadowMount backups registered, 0 missing.
- Cheat library `shipped=0` — all 7 022 already present, so the ELF correctly wrote nothing.
- All 12 PC and console endpoints 200. `app.db` integrity `ok`, 109 games.
- etaHEN's log: **no bind errors at all.**

`POST /api/hosts/cleanup` was exercised for real and stopped `garlic-savemgr`, `dpi.elf`,
`nanodns.elf`, `ftpsrv.elf`, `klogsrv`, `tile-autoinst` while leaving etaHEN and ShadowMount up.

Elf Arsenal is now also **disabled in Payload Manager** — its payload renamed to
`elf-arsenal.elf.disabled`, so it cannot be launched by hand or by accident. Reversible by renaming
it back. Note `nanodns` and `ftpsrv` come from `/data/pldmgr/autoload.txt`, **not** from Arsenal —
that attribution was wrong earlier.

### Not changed, deliberately

The audit's second candidate is that our proof is size-and-existence only — never content, never
freshness. That is true and worth fixing, but candidate A and candidate B demand *different* fixes,
and a confident wrong fix is the worst outcome here. **Three measurements settle it** on the
recovered console, before any further change: the `last_updated` on `CUSA14409`'s `bgft` rows (a
2026-08-21 timestamp means the install was real and the fault is at launch; 2026-08-03 means the
proof was stale; more than one row means a duplicate submission), the file **mtimes** under
`/mnt/ext1/user/app/CUSA14409/`, and a sha256 of the console's `app.pkg` against the PC's.

The source package is not in doubt: `pkg_completeness` reports it complete and confident, and that
exact file installed through Arsenal on 2026-08-03 and was confirmed playable.

## [3.20.0] — 2026-08-22 · "etaHEN can drive the install engine, and the two hosts stop fighting" `[VERIFIED]`

### Added — etaHEN 2.6B as a first-class install host

:12800 can now be served by **either** Elf Arsenal or etaHEN, and the app detects which and speaks
the right protocol. They are not interchangeable — this was measured on hardware, not assumed:

| | Elf Arsenal | etaHEN 2.6B |
|---|---|---|
| endpoint | `POST /api/install`, JSON | `POST /upload`, multipart field `url` |
| success | `{"res":"0"}` | `SUCCESS: Direct install console Task started…` |
| failure | **queues, then rejects** | `FAILED: … SCE_PLAYGO_ERROR_…404_NOT_FOUND, code 0x80B22404` |
| bad URL | leaves a parked bgft job | **validates first — creates no bgft row at all** |
| HEAD / | answers | resets the connection |

Sending one the other's request is not a soft failure: etaHEN has no `/api/install` and simply
resets (measured `http=000` in 0.03 s).

etaHEN is the better host for us. It reports the console's real Sony error instead of a generic
rejection, and because it validates *before* queueing it cannot produce the duplicate-bgft-job
hazard that makes an Arsenal rejection un-retryable (invariant I4).

- `dpi_host_kind()` — detects the owner of :12800 from its own page, cached 20 s. `dpi.host` in
  config pins it to `arsenal`/`etahen`; default `auto`.
- `_install_etahen()` — the multipart client, parsing the real `SCE_*` code out of a FAILED reply.
- `dpi_install_ready()` — etaHEN has no `/api/install`, so the Arsenal junk-POST probe reported a
  healthy etaHEN as wedged. It now asks for the page instead. Deliberately **not** `/upload`: with
  etaHEN that endpoint is a real, logged install attempt, not a probe.
- `dpi_live()` — same problem, same fix. etaHEN does not implement HEAD, so the HEAD probe read
  `wedged` on a perfectly healthy host. GET for etaHEN, HEAD for Arsenal.
- `ensure_install_host_safe()` — Arsenal-only, now returns immediately for etaHEN (8 ms instead of
  an FTP round-trip). etaHEN has no dashboard tile of its own to re-queue, so it cannot hit that bug.
- `ensure_dpi_host()` — will no longer launch Elf Arsenal when etaHEN already serves. Doing so
  started a second DPI v2 that could never bind, plus nanodns, garlic and the tile auto-installer.
- `recover_dpi()` — etaHEN's DPI v2 lives inside its Utility daemon, which also serves FTP and klog.
  Killing that to "reload the installer" would take those down with it and there is no separate
  payload to relaunch, so it now reports honestly instead of doing damage.

### Fixed — the stale-`via` hazard the host switch created

`last_install_via()` reads Elf Arsenal's `last-install.json`. etaHEN never writes that file, so with
etaHEN serving it still returned `"sceAppInstUtilInstallByPackage(url)"` from an Arsenal install
hours earlier — a stale verdict on an unrelated install. It now returns nothing when the host is not
Arsenal, and takes a `since` timestamp so a record older than the current hand-off is ignored.

### Fixed — Arsenal and etaHEN were fighting over :12800 forever

etaHEN's util-daemon log was **2 MB of one repeating pair of lines**:

    Notify: [etaHEN] DPIv2 error: bind | Address already in use
    Serving on http://10.0.0.99:12800 (eth0)

Arsenal's `dpiv2.elf` had the port, so etaHEN retried in a tight loop indefinitely. And it is not
recoverable in the other direction either: once etaHEN's `DPI_v2` is on, its retry loop reclaims the
port the instant Arsenal's dpiv2 dies — verified by reloading Arsenal 15 times in a row, etaHEN won
every time. **They cannot coexist; exactly one install host may run.**

`payload_bundle.h` now carries an explicit `port` per bundled payload and `payload_bootstrap()`
honours it, replacing the old `name == "shadowmount" ? 9021 : 12800` ternary. `port == 0` means
*ship the file, never auto-start it*, so we can never start a second install host.

### Changed — Elf Arsenal is no longer bundled; etaHEN is the install host

Arsenal and etaHEN both serve DPI v2 on :12800 and **cannot coexist**. Running both is what put a
wall of `DPIv2 error: bind | Address already in use` notifications on the TV and grew a 2 MB log.
So Arsenal is out of the payload bundle entirely.

The bundle is now `shadowmount` (:9021) and `etahen` (:12800), each carrying the port it serves.
`payload_bootstrap()` starts a payload only when its port is free, so if etaHEN is already running
from its own app we leave that instance completely alone — verified live:
`{"bundled":2,"status":"shadowmount=already-running,etahen=already-running"}`.

Dropping Arsenal also drops what it dragged in: `nanodns`, `garlic-savemgr`, `dpi.elf` and the tile
auto-installer that crashes SceShellCore's job queue (the thing `ensure_install_host_safe()` exists
to defuse).

- **etaHEN.elf** (4 763 312 B) bundled and auto-started when nothing owns :12800.
- **shadowmountplus.elf** refreshed 1 688 528 → 1 746 608 B.
- **elf-arsenal.elf** removed from the repo and the ELF (−4.7 MB).

**FTP is no longer assumed to be on 2121.** Arsenal's `ftpsrv` served that; etaHEN's FTP is on 1337.
The ELF now probes with `ftp_live_port()` (2121, then 1337, else 0) everywhere it used to hardcode
2121 — `/api/health`, `/api/helpers`, `/api/devices` and the readiness toast.

### Added — the cheat library ships INSIDE the ELF

7 022 files (30.7 MB: json 1994, mc4 2142, shn 1763, patches 376, xml 369, xml_orbis 369,
xml_prospero 9) are embedded in the payload itself, so installing the app is the only thing anyone
has to do — no separate download, nothing to copy to the console.

`gen_cheat_bundle.py` packs every file into one `cheats.pack` and emits a table of
`(path, offset, length)`, pulled in with a **single `.incbin`**. It is deliberately not shaped like
`gen_web_bundle.py`, which emits octal C string literals: that is fine for 881 KB of UI but would
turn 30.7 MB into roughly 120 MB of C source. The packer runs in 0.57 s.

`cheat_bundle_extract()` writes them on boot and **skips anything already on disk**, so the first
boot lays the library down and every later boot costs one `access()` per entry and writes nothing.
Verified: deleted 40 files across four folders, reloaded the ELF, and it reported
`shipped=40` with all 7 022 present again.

ELF: 8 987 424 → 41 880 456 B. That is the deliberate cost of self-containment — the loader maps
the payload into RAM, so the library is carried in memory to avoid ever asking the user to fetch it.

The library is also bundled in the exe (+10.8 MB) and `start_cheat_sync_thread()` pushes anything a
console is missing over FTP — incremental, time-budgeted to 90 s so it can never monopolise the link
installs depend on. That is the fallback for a console running an older ELF; the embedded copy is
the primary path. `GET /api/cheats/library` reports local vs console per folder,
`POST /api/cheats/sync` runs it.

### Added — it will not reinstall what is already there

Reinstalling is never harmless: for a base game the console **skips the re-pull and keeps the old
files** (the `stale_install` trap), and for an add-on it is a wasted transfer. Each kind is recorded
in a different place, so each needs its own proof:

| kind | proof it is already installed |
|---|---|
| base game | registered in `app.db` **and** its own `app.pkg` on disk at ≥98% |
| update / patch | `app.db` `APP_VER` already at or past the package version |
| DLC | its `content_id` present in `addcont.db` |

`installed_addons()` reads `addcont.db` — the only record a DLC leaves, since a DLC never appears in
`app.db` and never gets an `app.pkg`. Nothing read that file before.

The check runs before anything is queued and answers `{"ok":true,"skipped":true,
"reason":"already_installed","message":…}`; the UI shows the message and marks the button
*Already installed*. A failed lookup never blocks an install. `force:true` overrides.

Verified live — all three skip paths, and a genuinely new package still installing:

    DLC already in addcont.db  -> "this add-on is already installed on PS5"
    update already applied     -> "KNACK 2 is already at version 01.01"
    base game present          -> "CUSA13529 is already installed on PS5 — delete it there first…"
    a NEW DLC                  -> queued -> transferring -> promoting -> playable, addcont.db 5 -> 6

### Fixed — FTP no longer assumes Arsenal, and a button to clear the port fight

Dropping Elf Arsenal takes its `ftpsrv` (:2121) with it, and **every** read of console state goes
over FTP — app.db, bgft.db, addcont.db, the install proof, the cheat sync. `Ps5Bridge._ftp()` and
`ftp_ok()` now try the configured port and fall back to the other of 2121/1337, remembering which
answered. Verified: etaHEN's :1337 serves all three databases, `LIST`, and `STOR` writes, and the
41.9 MB ELF was deployed over it in 5.9 s.

**`POST /api/hosts/cleanup`** and a *Fix install-port conflicts* button in Settings stop Elf Arsenal
and the services it leaves behind (`dpiv2`, `dpi`, `nanodns`, `ftpsrv`, `tile-autoinst`,
`garlic-savemgr`, `klogsrv`) while **leaving etaHEN and ShadowMount running**. That is a different
job from the rest-mode sweep, which stands everything down for suspend.

It matches specific process names only. Payload Manager reports **every** loaded payload as
`payload.elf`, including our own ELF — killing by that name kills the app. (Learned live: an ad-hoc
sweep took our own ELF down with Arsenal's orphans. The app's own `REST_STOP` never had this bug —
it is an explicit allow-list and `rest_scan` already excludes `getpid()`.)

`REST_STOP` gains `etahen`/`eta-hen`/`eta_hen`, `onion_daemon`, `onion_util` — etaHEN is now the
install host and is exactly the long-lived homebrew that made suspend panic. `REST_KEEP` gains
`onion_elfldr` explicitly: loaders must never be stopped or the console is stranded.

### Fixed — arbitrary file read on the PC companion `[SECURITY]`

`_static()` is the catch-all for every unmatched GET, and on Windows
`os.path.join(WEB_DIR, "C:/Windows/win.ini")` **discards `WEB_DIR` entirely** because the drive
letter makes the path absolute — so the `".."` guard never saw it. The companion binds `0.0.0.0`,
so anything on the LAN could read any file on the PC. Verified before the fix: `GET
/C:/Windows/win.ini` → **HTTP 200 with the file body**.

Now the joined path is resolved with `realpath()` and required to sit inside `WEB_DIR`, which covers
drive letters, UNC paths and symlinks in one check. After: `400`, while `/`, `/index.html`,
`/assets/logo.png` and `/config.js` all still serve normally.

### Fixed — one idle TCP connection froze the whole console API `[SECURITY]` `[RELIABILITY]`

The console's accept loop is single-threaded and its `read()` had no timeout, so a client that
connected and sent **nothing** blocked it indefinitely — taking the UI, the install engine and the
cheat engine down with it. A dead browser tab or a port scan was enough. Measured: `/api/health` OK
in 0.01 s, one idle socket opened, then timeouts for as long as it was held, recovering the instant
it closed.

Accepted sockets now get `SO_RCVTIMEO` 8 s and `SO_SNDTIMEO` 30 s. The long transfers are unaffected
— `/pkgfile/` is handed to a worker thread with its own socket before this read.

### Verified on hardware

etaHEN serving :12800, six inFAMOUS Second Son DLC installed through the full app path —
`addcont.db` 0 → 6 rows with real content ids, fresh `1026` bgft rows each time. `dpi_state` reads
`live`, `/api/dpi/status` `ready:true`, etaHEN's log flat at 0 bytes/8 s (the bind-loop is gone),
`app.db` integrity `ok`. Every PC and console endpoint answering 200.

Then a full base game, deleted first and reinstalled through the app with etaHEN serving and Elf
Arsenal stopped: **Castle Crashers, 217 MB, `queued → submitting → transferring → promoting →
playable` in 21 s**, `app.pkg` byte-exact at 227 540 992, all five files present, bgft `1036`,
`app.db` integrity `ok` — and a second attempt correctly refused with *"already installed"*.


## [3.19.0] — 2026-08-05 · "The install host has its own fallback, and it was lying to us" `[VERIFIED]`

### Fixed — an add-on could report "Ready to play" having installed nothing at all

Reported as *"installing a DLC or update sometimes fails with HTTP 400"*. Reproduced on hardware and
root-caused; it is not the drive, and it is not a 400 the app ever sees.

**What actually happens.** Elf Arsenal runs its own fallback ladder that we do not control. When
`sceAppInstUtilInstallByPackage` fails it silently retries with `sceAppInstUtilAppInstallPkg` — the
metadata-only call — and still answers `ok:true`. Measured, same package, twice in a row:

    attempt 1   reloading ⟳ payload launched      via: sceAppInstUtilAppInstallPkg      contentId: ""
    attempt 2   reloading ⟳ stopping wedged DPI   via: sceAppInstUtilInstallByPackage   contentId: UP5245-...

Attempt 1 stamped `APP_VER 01.22` into `app.db` for Subnautica, wrote **no `bgft.db` row**, moved
**zero bytes** — and the app said *Ready to play*. It is transient: it happens on a daemon that was
just reloaded, which is why it looked random. The retry installed for real.

Two of our own checks let it through:

- **`last_install_error()` never read `via`.** It looked at `error` and `pending` only, so the
  fallback was invisible. Now `last_install_via()` reads it and **anything that is not
  `InstallByPackage` means nothing was installed** — the task is failed, the host is marked dirty,
  and it is retried once on a freshly reloaded daemon before giving up with `metadata_only`.
- **The add-on proof gate was structurally incapable of failing.** It compared the *base game's*
  `app.pkg` against 98% of the *add-on's* size — for an 8.1 MB update that is 2.9 GB vs 8.3 MB, true
  before the add-on has done anything. An add-on now needs **its own new completed `bgft.db` row**:
  a row that differs from the snapshot taken before hand-off, fully transferred, with a terminal
  status. `1036` = a full title install, `1026` = a patch/update install.

Also: a failed add-on no longer tells you to delete the game. It was falling into the
`stale_install` branch — whose advice is *"delete it on the console and install again"* — because
the parent title is, of course, already registered. New `addon_not_confirmed` says the base game was
left alone. And `remember_installed()` is no longer called for an add-on; that file tracks titles.

Verified on hardware at 3.19.0: two inFAMOUS Second Son DLC installed through the new gate,
`via: InstallByPackage`, and `addcont.db` went 0 → 2 rows with the real content ids.

### Not changed — install destination, and why

Also asked for: pick the destination drive for PS4 PKG installs in the app. **There is no mechanism
to do it.** `MetaInfo` (`server.c:4799`) has no destination field, the DPI POST body is literally
`{"url": …}`, and not one of the six `sceAppInstUtil*` symbols we hold names a device, location or
target. `-lSceRegMgr` is linked but never called — the RegMgr strings in our ELF come from the
embedded Arsenal binary, not our code — and the registry key id for Installation Location exists
nowhere in this repo. Guessing one is a blind write to an unknown system setting with uid 0.

What was tested instead: **add-ons are not drive-bound.** Four add-ons, two per drive, with the
console's own Installation Location set to Internal throughout:

| title | drive | `via` | patch row |
|---|---|---|---|
| PAW Patrol `CUSA52195` | internal | InstallByPackage | `1026` ✓ |
| Castle Crashers `CUSA14409` | internal | InstallByPackage | `1026` ✓ |
| KNACK 2 `CUSA08014` | **ext1** | InstallByPackage | `1026` ✓ |
| Subnautica `CUSA13529` | **ext1** | AppInstallPkg → retry → InstallByPackage | `1026` ✓ |

Two extended-storage add-ons installed with the console defaulting to internal. Sony's installer
does follow the base game. Base PKG installs still follow the console's own setting.

### Changed — the queue is a pill in the header, not a bar across the bottom

It was docked to the bottom of the window at `position:fixed`, overlaying the library, and it grew
taller as jobs were added — so the more there was to watch, the less library you could see. `main`
carried 84px of bottom padding purely to clear it.

Now it is a compact pill in the middle of the header with a **conic-gradient progress ring**, and
the list is a floating drawer that opens on hover and pins on click. The drawer is out of flow, so
the grid never reflows. Same information, same ids, same handlers — `#dock`, `#dockhead`,
`#dockbody`, `#qn`, `#startQueue`, `#reloadDpi`, `#clearDone` all unchanged.

The ring is one CSS variable, so an update is a paint and never a layout — it ticks every 1.2s
while installs are live. It reads: idle · sweeping while transferring · spinning when the percentage
is not yet meaningful or the engine is self-healing · amber held · red failed · green done. The
label shows the game name for one job and *"N installing"* for several.

### Changed — filter rows stacked, search narrowed, console picker hidden

The two chip rows sat side by side and squeezed the storage tiles into a wrapping strip. Stacked
into one column they take 366px instead of ~900px at 1600px wide, and `#drives` gets the rest —
measured 1130px, four tiles on one line. Chips and the sort select are tighter; the search box went
from `max-width:420px` to `flex:0 1 260px`.

The console picker is hidden while there is only one console (it was a dropdown with one entry
sitting next to a status pill that already names the console). It reappears automatically for a
second console. `state.console` is still set exactly as before, so every install POST targets the
same console it always did.

## [Verified] — 2026-08-03 · 3.18.2 proven end-to-end on hardware `[VERIFIED]`

No code changed. This entry records the hardware verification of 3.18.2 after the Y2JB re-run, and
one deployment trap found along the way that had been silently serving an old build.

### The console was running 3.18.0, not 3.18.2 — and nothing said so

The Desktop `.elf` was 3.18.2. The new build was uploaded to
`/data/pkg-mutant-shop/payloads/PKG-MUTANT-SHOP.elf` and loaded with
`GET :8084/loadpayload:<that exact path>`, which answered `OK`.
`GET :8710/api/health` then reported **`"version":"3.18.0"`**.

**Payload Manager resolves `/loadpayload:` by BASENAME against its own registry**
(`/data/pldmgr/payloads/<NAME>/<NAME>.elf`), not by the path given. Its registered copy was a stale
3.18.0 — 8 971 112 B against our 8 971 040 B. 3.18.0 is the build with the AppInstallPkg fallback
that produced the two unplayable titles and took the console down.

`SHOP_VERSION` is compile-time only, with no runtime override, so `/api/health` cannot report a
version the binary does not contain. That makes it the authoritative check — and it is the only
reason this was caught. **A version on the Desktop is not the version on the console.**

Deploy procedure that actually works:

1. back up `/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf`, then overwrite it
2. `GET :8710/api/quit` — our own port-handover endpoint; poll until `:8710` drops
3. `GET :8084/loadpayload:/data/pldmgr/payloads/PKG-MUTANT-SHOP/PKG-MUTANT-SHOP.elf`
4. `GET :8710/api/health` and confirm the version **before testing anything**

Still open: nothing in this repo writes to `/data/pldmgr/payloads/`, so step 1 is manual.

### Install lane verified — two games, back to back, no wedge

Lane: PC serves the PKG on `:8710` → `POST :12800/api/install` (Elf Arsenal DPI v2) →
`sceAppInstUtilInstallByPackage(url)` → Sony's BGFT.

| | Riptide GP2 `CUSA02365` | Castle Crashers `CUSA14409` |
|---|---|---|
| `app.pkg` | 107 806 720 / 107 806 720 | 227 540 992 / 227 540 992 |
| `app.pbm` · `.backup` · `app.json` · `app.xml` | all present | all present |
| `bgft.db` `tbl_downloads.status` | **1036** | **1036** |
| Arsenal `last-install.json` | `{"ok":true,"result":0,"via":"sceAppInstUtilInstallByPackage(url)"}` | same |
| `app.db` | registered, correct `metaDataPath` + `contentId` | same |

Both landed on `/mnt/ext1/user/app/<TID>/`. The second install was caught mid-flight:

    [  0s] state=submitting  "Handing off to PS5"
    [  4s] state=promoting   bgft status=1009  219 283 456 / 227 540 992
    [  8s] state=playable    bgft status=1036  227 540 992 / 227 540 992

**`tbl_downloads.status` 1009 → 1036 is the signal that drives the PS5's own top-right toasts**
(Downloading → Installing → Ready to play, with box art). Watch that table to prove the
notifications fired, rather than relying on anyone having been looking at the screen.

`dpi_install_ready()` stayed `true` after both installs — the wedge that used to follow the first
install did not occur. `dpiv2.elf`, `dpi.elf`, `garlic-savemgr`, `shadowmountplus.elf` and
`kstuff.elf` all survived. `app.db` `PRAGMA integrity_check` = `ok`.

Both titles were confirmed playable by the user afterwards.

### Corrected — `app_install_all` is not the registration switch it was recorded as

The Y2JB re-run reset `/data/shadowmount/config.ini` to the stock all-commented template. Its own
runtime log then showed `app_install_all=0`, while **all 22 `.ffpfsc` backups on
`/mnt/ext1/homebrew` were registered, 0 missing.**

The template claims the value is *"forced to 1 on FW 12.00 and newer"*; the log disagrees. Earlier
notes said to force it to 1 to fix registration. On this ShadowMount build registration works at
`0`, so **count actual registrations before changing it** — the value alone proves nothing either way.
It was left untouched.

### Housekeeping

- Both broken registrations from 3.18.0 (`CUSA02365`, `CUSA14409`) were already gone; the DB rebuild
  cleared them. `/user/app/` and `/mnt/ext1/user/app/` held no leftovers for either.
- Removed two stale cached PKGs from `/data/pkg-mutant-shop/install/` after confirming both matched
  the PC library byte-for-byte. **`Star-Wars-Racer-Revenge-LUA-C0RE-EXPLOIT.pkg` (582 549 504 B) was
  kept** — it exists only on the console; the library entry showing it is our own console-merge, not
  a PC copy. Deleting it would lose it.
- The engine is now documented in full in **`MUTANT PKG ENGINE.md`** — the rebuild manual. Repair the
  engine from that document, not from memory or from this changelog's older entries.

## [3.18.2] — 2026-08-02 · "'Ready to play' now requires the game to actually be there"

### Fixed — the check that let an unplayable install be reported as finished
For a title that was not already registered, the queue accepted **presence in `app.db` alone** as
proof of a completed install:

    if tid in installed_titles():
        if not was_registered:
            installed = True        # <- app.db row is enough

A metadata-only registration produces exactly that row, with no game data behind it. So the app
could show *"Ready to play"* for a title that crashes the console on launch — which is precisely
what happened with the two titles installed by 3.18.0.

Every install, new or repeat, now has to show the game's own **`app.pkg` on disk** at 98% or better
(`/user/app/<TID>/` or `/mnt/ext1/user/app/<TID>/`) before it is called installed. The app is no
longer capable of reporting a data-less registration as a success.

## [3.18.1] — 2026-08-02 · "Reverting 3.18.0 — AppInstallPkg does not install a game"

**3.18.0 was wrong and it broke a console.** It made `sceAppInstUtilAppInstallPkg` a fallback in the
game install path. That call registers metadata and no game data: the title appears installed, the
tile shows, and launching it takes the console down hard enough to need Y2JB re-run. Two titles were
installed that way and both are unplayable.

The evidence was already in this file, in three places — including inside 0.19.0's own **Fixed**
section, the *other* half of which was quoted:

    0.19.0  "sceAppInstUtilAppInstallPkg registers metadata only - it creates /user/appmeta/<TID>
             and no game data, which is what produced dashboard tiles that failed with
             'Cannot start the game'. It is no longer used as an install path."
    2.6.0   "The USB path called sceAppInstUtilAppInstallPkg, which only registers metadata... the
             tile then fails with 'Cannot start the game'."
    3.9.4   "hands off through dpi_install_url() - NOT sceAppInstUtilAppInstallPkg, which only
             writes metadata and is what produces an unplayable tile."

### Reverted
- **The AppInstallPkg fallback is gone from the game path.** It remains only for our own deeplink
  tile, which has no data to install. A real game must go through `InstallByPackage` / BGFT — which
  is also the only path that produces the PS5's own *Downloading → Installing → Ready to play*
  notifications with the game's artwork.
- **`dpi.mode` is back to `v2`**: PKG served on the LAN -> `POST :12800/api/install` -> Elf Arsenal
  DPI v2. That is the mechanism with the strongest repeated verification in this changelog.

### Recorded so it is not lost again
A real install lives at **`/mnt/ext1/user/app/<TID>/`** (Extended Storage, `contentLocation=2`) with
`app.pkg` + `app.pbm` + `app.pbm.backup` + `app.json` + `app.xml`. `/user/appmeta/<TID>` alone proves
nothing — that is exactly what a metadata-only registration leaves behind. Always check BOTH roots,
and check for `app.pkg`, before calling anything installed.

## [3.18.0] — 2026-08-02 · "Our own install engine is switched back on"

Installs were broken by two regressions away from what **0.19.0 — "Our own install engine — Elf
Arsenal no longer required"** had already built and verified. Both are undone. Nothing new was
invented here; this restores what worked.

### Regression 1 — the install lane had been switched back to Elf Arsenal
0.19.0 set `dpi.mode` to **`pms`** so installs run on our own engine. The default in `server.py` had
reverted to `"v2"`, and both `config.json` files with it, so every install was being handed to
Arsenal's DPI daemon — which queues onto the console's BGFT installer queue, the queue Arsenal's own
tile re-install crashes. Our engine sat idle and unused the whole time.

Back to `pms`, in the code default and both configs. **Our engine never enters that queue at all** —
0.19.0 noted it as a limitation ("installs do not appear in the PS5 download list"), and it is
exactly why it keeps working when the queue is wedged.

### Regression 2 — the engine's final step used the call that cannot work
0.19.0: *"register it with `sceAppInstUtilAppInstallPkg`, **the one call that works from this
process***". That had been replaced with `install_full()` (`InstallByPackage`) alone, on the belief
that AppInstallPkg "only registers metadata". It answers **0x80B2116F** every time, so every install
died after a perfect 100% download.

The ladder is restored — InstallByPackage first, **AppInstallPkg as the fallback** — which is what
Arsenal does too, and for the same reason: InstallByPackage is unreliable for everyone. The
"metadata only" reading was wrong because game data lands in *managed storage*, which is not
browsable over FTP; an empty `/user/app/<TID>` proves nothing.

### Added — the engine now proves the install instead of trusting a return code
After the ladder it reads the console's own title list back and waits for the title to appear.
`rc = 0` with no registration is reported as a failure, not a success, and the message says which
call was used:

    done  100%  107,806,720/107,806,720  rc=0x00000000  "Installed via AppInstallPkg"

### Verified end to end on hardware
**Riptide GP2 (CUSA02365)** — downloaded 107,806,720 bytes to console storage, installed, and
registered:

    app.db    CUSA02365 · "Riptide GP2" · /user/appmeta/CUSA02365 · categoryType 61440
    appmeta   icon0.png, icon0.dds, pic0/pic1, snd0.at9, shareparam.json, save_data.png
    library   on_console = true

## [3.17.1] — 2026-08-02 · "Two ways the queue could lie about an install"

Both found by an end-to-end audit of the install lane and confirmed in the source.

### Fixed — with the console OFF, the app reported a game as installed. Permanently.
If the PS5 was unreachable the lane animated progress to 100%, said **"Installed (simulated)"** and
then called `remember_installed()`, writing the title into `installed.json` for good. The library
then showed a game as installed on a console that had never received it. The demo animation is gone;
an unreachable console is now an honest error that installs nothing and remembers nothing.

### Fixed — a transfer that died part-way hung that console's queue forever
The stall detector was `stalled = stalled + 1 if pct == 0 else 0` — it only counted while progress
was **zero**. The moment a single percent landed it reset, so a transfer that stopped at 3% held the
worker open indefinitely, and because installs are serialised per console that console's entire
queue was blocked with no way out but restarting the app.

It now tracks real byte progress: no NEW bytes for 90s is a stall at any point in the transfer, and
a part-way stall reports the percentage it died at instead of pretending it is still going.

## [3.17.0] — 2026-08-02 · "The install host was crashing the console's installer queue"

### Root cause — found in Elf Arsenal's own source
Installs were accepted, queued, and then transferred **zero bytes, forever**. It was never our
package, our file server, or the DPI daemon. Elf Arsenal ships:

    static atomic_int g_tile_autoinstall     = 1;   /* default on */
    static atomic_int g_tile_skip_if_installed = 0; /* default: always reinstall */

so ~30s after every start it re-queues its own 1.5 MB dashboard tile — even when that tile is
already installed. An earlier build guarded against this unconditionally, and the comment on that
removed guard says exactly what it costs:

    // Skip if PSPS69691 tile is already installed - avoids triggering
    // AppPrepareOverwriteByPackage every boot, which crashes SceShellCore's
    // SceAppInstallerJobQueue via an uncaught std::out_of_range.

Upstream deleted that guard and demoted it to an opt-in flag that defaults OFF. Once the job queue
is crashed, every install afterwards sits at `transferred = 0`. That is why this used to work and
then stopped: the behaviour changed in the install host, not here.

Proof from the console's own `bgft.db` — the **same job, same content id, same size**:

    completed  item 3   19:44:24   transferred 1,572,864 / 1,572,864
    stuck      item 37  23:01:52   transferred         0 / 1,572,864

### Fixed — the app now protects the install host from itself
`Ps5Bridge.ensure_install_host_safe()` runs before every hand-off: it sets `tile_autoinstall=0` and
`tile_skip_installed=1` in `/data/elf-arsenal/config.ini` (survives a reload) and applies the same
through the live API at `:6969/api/state` (takes effect immediately). Idempotent — a no-op once
correct, and it never overwrites a config it failed to read.

### Fixed — one Install was creating THREE install jobs
The DPI retry policy said a `res != ok` reply meant "nothing was queued, safe to retry". It does
not. The daemon queues the install the moment it accepts the POST, polls app.db for up to 90s, and
only then answers `{"res":"-1","error":"install failed"}` — so a rejection means *queued and did not
finish*. Retrying re-submitted the same package: measured on hardware, one Install produced **three
`bgft.db` jobs for the same content id within 36 seconds**, all stuck at 0, duplicates blocking each
other.

A rejection is now terminal, and the read timeout went from 15s to 120s so the daemon's real verdict
is actually seen instead of being guessed at. Verified after the fix: `jobs_for_title = 1` for the
whole run, where it had been 3.

### Note
Both fixes stop this recurring. The queue that is *already* crashed lives inside SceShellCore and
only comes back when SceShellCore does.

## [3.16.1] — 2026-08-02 · "The tile installs; and an install failure now says what really happened"

### The dashboard tile installs, live, with no reboot
`sceAppInstUtilAppInstallPkg` returned **0x00000000** and the system installer created
`/user/appmeta/PKGM00001/{param.json,icon0.png,icon0.dds}` itself — the `.dds` is generated by
ShellCore, which is the proof it went through the real installer.

    metaDataPath   /user/appmeta/PKGM00001
    categoryType   65536    dispLocation 188     -> Media apps
    deeplinkUri    http://127.0.0.1:8710/        -> works with every PC off

Re-running answers `already registered`; reloading the ELF leaves `PKGM00001 rows = 1`. The package
is embedded in the ELF (`tile_bundle.h`, `.incbin`) and built by our own recovered LibProsperoPkg
toolchain — 1,324,322 bytes against the original's 1,324,320.

**The earlier `0x80A40029` was never the package.** It was the console's damaged app database: the
jailbreak was only half-working while `appinfo.db-corrupt` was latched. After Y2JB was re-run and the
database rebuilt, the identical ELF installed the identical package first try. If AppInstUtil answers
`0x80A4xxxx`, suspect console health before suspecting the package.

### Fixed — "DPI daemon wedged" was the wrong answer to almost every install failure
The queue had exactly one explanation for a failed install and used it for all of them. Elf Arsenal
writes the real reason to `/data/elf-arsenal/last-install.json`, and it is usually something else
entirely:

    "error": "install queued but CUSA02365 did not appear in app.db after 90s",
    "via":   "sceAppInstUtilInstallByPackage(url)",
    "contentId": "EP0786-CUSA02365_00-RIPTIDEGP2PS4001"

That content id was read out of **our** package, so the file was fetched, parsed and accepted — the
transport was never the problem. The console's own install queue simply never ran the job.
`Ps5Bridge.last_install_error()` now reads that file and the hold message says what actually
happened, instead of sending someone to reload a daemon that is working perfectly.

### Diagnosed — the console's install queue is jammed (not an app fault)
`/system_data/priv/mms/bgft.db` shows `download_inprogress = 4`, with four jobs parked at status
1000: Star Wars (CUSA03474), Castle Crashers (CUSA14409), Riptide GP2 (CUSA02365) and **Elf Arsenal's
own tile (PSPS69691)**. Every `InstallByPackage` job now stacks up behind them. Arsenal cannot install
its own tile either, which is why its tile never came back after the database rebuild.

Our tile was unaffected because `AppInstallPkg` registers metadata and never enters that queue.
Clearing the stuck entries from the console's Downloads screen is what frees it.

## [3.16.0] — 2026-08-02 · "The tile, the rest-mode sweep, and a heap overflow that was corrupting memory"

### Found — `sceAppInstUtilAppInstallTitleDir` does not exist on FW 12.70
The dashboard tile could not be registered, and no amount of fixing the call would have helped.
Probed live against the console's own module list:

    sceAppInstUtilInitialize          0x80035e5d0   OK
    sceAppInstUtilAppInstallPkg       0x80035ea60   OK
    sceAppInstUtilAppInstallAll       0x80035ea80   OK
    sceAppInstUtilAppInstallTitleDir  0x0           NOT EXPORTED (by name AND by NID)

New endpoint `GET /api/engine/sym?name=&nid=&mod=` does this lookup on demand — a NID is a hash, so
a wrong one is indistinguishable from a missing function, which is what made this invisible before.
Resolution now also tries `kernel_dynlib_dlsym()` by real symbol name; that is how `AppInstallAll`
and `AppUnInstall` became available (the latter had been hardcoded to 0, so patches could never be
reverted through it).

### Fixed — the tile is back, and now survives the PC being off
`/user` was wiped when the console was set up fresh, taking `/user/app/PKGM00001` and
`/user/appmeta/PKGM00001` with it, while boot-time registration had been removed on the assumption
the tile was "already installed". Registered again via the repo's own `register_tile.py` (pristine
on-console backups kept, `PRAGMA integrity_check` on both databases, atomic RNFR/RNTO replace,
`--restore` available). Verified in `app.db`:

    titleId PKGM00001 · titleName "PKG MUTANT SHOP" · categoryType 65536
    pprDeeplinkUri  http://127.0.0.1:8710/
    pprHubAppUri    psmediahub:main?titleId=PKGM00001      <- what puts it in Media apps

The deeplink points at **127.0.0.1**, not at a PC. The old build's eboot had a specific LAN address
compiled into it, so the tile was dead whenever that machine was off. Both working homebrew tiles on
this console (Elf Arsenal, Homebrew Launcher) are localhost deeplink tiles with no eboot at all —
this now matches them.

`GET /api/tile/status` and `/api/tile/install` were added, and boot restores the tile only when it is
genuinely missing (the unconditional version is what used to re-create dashboard entries). It never
toasts a failure — on this firmware the on-console attempt cannot succeed, and an error popup on
every boot is worse than silence.

### Fixed — a heap overflow in the peer library merge
`*len += snprintf(...)` accumulated snprintf's **untruncated** return against a 6000-byte headroom
guard, while one record's worst case is ~8.2 KB. Once `*len` passed `*cap`, the size_t subtraction
`*cap - *len` wrapped to a huge number, the guard passed, and the next record wrote past the end of
the heap block with an effectively unlimited size. One PC with long game names was enough, and this
runs on every peer merge. Now: reserve 16 KB, write into the real space, advance only by what landed,
and drop a record that still will not fit rather than carry half of it.

### Fixed — four threads with no stack size
PS5 default worker stacks are tiny; five threads already set 256 KB with comments saying so. The four
that did not were the riskiest: the download/install worker, the InstallByPackage worker, the toast
thread, and **the boot bootstrap — the one that runs on every wake**, while the console is still
assembling itself.

### Fixed — the rest-mode sweep believed a helper that lies
Payload Manager answers `{"ok":true,"message":"Killed"}` whether or not the process died. Verified on
hardware: **`dpi.elf` survives three consecutive kills with the same pid**, while every other payload
dies on the first. The sweep counted "Payload Manager replied" as success, so the app could say
*"Rest mode should be safe now"* with homebrew still running into suspend.

Now it asks Payload Manager, sends `SIGKILL` itself as root, re-reads the process list, and repeats up
to three passes. What it reports is what is genuinely gone — `failed[]` means still running.
`tile-autoinst` and `klogsrv` were added to the stop list: after the console rebuild they were the
only homebrew still alive through suspend, and klogsrv holds a kernel log socket open. `kstuff` is
still deliberately never touched.

The TV toast and the app's own exit are now conditional on the sweep actually succeeding; both used
to fire even when Payload Manager never answered and nothing was stopped. A rest request during an
active download or install is refused (`force=1` overrides) — it used to `_exit(0)` mid-write, and a
move writes its final filename straight into ShadowMount's watch folder, so an interrupted one left a
half-written game to be auto-mounted on the next boot.

## [3.15.0] — 2026-07-31 · "A PKG can no longer arrive with a name the installer cannot fetch"

### Fixed — "DPI host answered but rejected this PKG — HTTP 500"
The install lane hands the console's DPI daemon a URL to the package. A filename carrying spaces or
brackets is not fetchable, and the daemon answers a bare `HTTP 500: Error` — **after** the queue has
accepted the job, so the failure surfaces at the worst possible moment:

    Cars 3 Driven to Win (CUSA07083) - [DLPSGAME.COM].pkg     -> HTTP 500

Packages are now renamed the moment they are seen, so a name that cannot work never reaches the
installer at all:

    Cars 3 Driven to Win (CUSA07083) - [DLPSGAME.COM].pkg
      -> Cars-3-Driven-to-Win-CUSA07083.pkg

The new name is built from the package's **own param.sfo** — real title, title id, version — not from
the old filename, so site stamps and release-group tags fall away while the game keeps its actual
name. Everything in the library was renamed on the first scan:

| before | after |
|---|---|
| `Cars 3 Driven to Win (CUSA07083) - [DLPSGAME.COM].pkg` | `Cars-3-Driven-to-Win-CUSA07083.pkg` |
| `Castle.Crashers.Remastered_CUSA14409_v1.00_[6.72]_OPOISSO893.pkg` | `Castle-Crashers-Remastered-CUSA14409.pkg` |
| `Castle.Crashers.Remastered_CUSA14409_v1.04_[6.72]_OPOISSO893.pkg` | `Castle-Crashers-Remastered-CUSA14409-UPDATE-v01.04.pkg` |
| `CUSA03173-Game-PRELUDE-[DLPSGAME.COM].pkg` | `Bloodborne-CUSA03173.pkg` |
| `CUSA03173_v1.09_1080p_60fps-[DLPSGAME.COM].pkg` | `Bloodborne-CUSA03173-UPDATE-v01.09.pkg` |
| `CUSA03173-DLC-PS4-[DLPSGAME.COM].pkg` | `Bloodborne-The-Old-Hunters-CUSA00900-DLC-v01.00-SPEXPANSIONDLC03.pkg` |

An update or DLC keeps its kind and version in the name on purpose: it sits in the same folder as the
base and must not collapse onto its filename — the library really does hold Castle Crashers as both a
227 MB base and a 10 MB update.

### It runs by itself
The folder watcher already rescans whenever the library changes, and the rename now happens at the top
of every scan — so a package copied into the games folder is fixed *before* it is listed, let alone
queued. A name that is already clean is skipped without even being opened, which is also what keeps
the watcher (which fires on name changes) from looping.

### Deliberately narrow
It touches **only** `.pkg` files inside the configured library folders. PS5 backups, the console, the
download/mount/install lanes and the cheat engine are not involved. Three guards before any rename:
a file modified in the last 15 seconds is left alone (still copying), a file a live queue job is
holding is skipped, and an existing name is never overwritten — a collision gets a numeric suffix.
`install_key` is rebuilt on every scan and never persisted, so renaming cannot orphan anything.

## [3.14.1] — 2026-07-31 · "Corrected against the real patch engines, before a single byte was written"

3.14.0 derived the patch format from the 376-file library. That got the value encodings right and
**two things wrong**. Both were found by reading the source of the engines that actually consume
these files — `ps-patch-system` (illusionyy's current PS4+PS5 engine, the only one implementing
`ImageBase`), GoldHEN's `game_patch`, etaHEN, and the shadPS4 emulator.

### Fixed — every PS4-style patch would have been written 0x400000 bytes too high
`Address` **already includes the load base**. The offset is `Address - (ImageBase, or 0x400000)`,
not `Address`. All four implementations agree:

    ps-patch-system  resolve_addr() = mapbase + (addr - base),  NO_ASLR_ADDR = 0x00400000
    etaHEN           PatchBaseAddresss = (ImageBaseAddr == 0 ? NO_ASLR_ADDR_PS4 : ImageBaseAddr)
    GoldHEN          addr_real = g_module_base + (addr_real - NO_ASLR_ADDR)
    shadPS4          std::stoi(patch.offsetStr, 0, 16) - 0x400000

The library says the same thing once you know to ask: of **19,167** addresses in files with no
`ImageBase`, the smallest is **0x401c22** and **not one** is below 0x400000. Image-relative
addresses would be scattered below it. Reading them as relative was self-consistent and wrong —
the corpus could not disprove it, only the reference sources could.

### Fixed — utf8/utf16 wrote one byte too many
The reference writes exactly `s.size()` bytes. 3.14.0 appended a NUL, which would have clobbered the
byte after the string. The library's utf8 lines *do* sit `len+1` apart — but that is where the
original data already had its terminator, not something the writer adds. A neat inference from data
that happened to be false.

### Added — `float64`, and signed decimals
`float64` is in the reference switch and was missing here (8-byte IEEE-754 LE). Scalar types now read
hex unsigned and decimal signed, matching `stoull` vs `stoll`, so `-1` means all-ones instead of
failing.

### Re-verified
All 376 files re-parsed on the console against the corrected rule: **0 errors, 0 mismatches,
19,223 applicable + 550 unsupported = 19,773 lines.**

Nothing had been applied to a game at 3.14.0 — no title has a version-matching patch, so the bug
never reached memory.

## [3.14.0] — 2026-07-31 · "The 376-file patch library is no longer invisible"

### New — game patches actually work
`"patches"` had been a hardcoded empty array in every response, so the whole migrated patch library
was dead weight and the panel's patch rows never rendered. The engine now reads the real format:

    <Patch>
      <TitleID><ID>PPSA01339</ID>…</TitleID>          up to 25 title ids share one file
      <Metadata Title= Name= Note= Author= AppVer= [ImageBase=]>
        <PatchList><Line Type="bytes" Address="0x0059ea79" Value="e9c2f41a01"/>

Each `<Metadata>` is one user-visible patch, and a single file carries up to **59** of them — usually
the same patch rebuilt for different game versions.

New endpoints on the console: `GET /api/patch/list`, `/api/patch/apply`, `/api/patch/revert`, and
`POST /api/mods/<tid>/apply`. Patches also appear in `/api/mods/<tid>` alongside cheats.

### The encodings were derived from the library, not assumed
`Line` has **ten** types, not one. The rules were read off the real data and then confirmed against
three independent sources:

| type | rule | how it was established |
|---|---|---|
| `bytes` | raw hex, verbatim | unambiguous |
| `byte` / `bytes16` / `bytes32` / `bytes64` | value, **little-endian** | "Resolution Patch (900p)" writes `0x00000640`/`0x00000384` = **1600**/**900**; the 720p variant writes **1280**/**720**. Verbatim those digits would be 1074003968. |
| `float32` | decimal → IEEE-754 LE | `0.016666667` is 1/60 — a 60 fps frame time. And `bytes32 0x3fe38e39` is IEEE-754 for 1.7777778, in a patch named "16:9 Aspect Ratio". |
| `utf8` | raw bytes **+ NUL** | in CUSA00547 the strings sit exactly len+1 apart — `CROW` at …63, `DENGEKI` at …68. Five of five agree. |
| `utf16` | UTF-16LE, Latin-1 widened | matches all three sources |
| `mask`, `mask_jump32` | **not supported** | `Address` holds a byte *signature*, not an address — it needs a code scan |

`ImageBase` appears in only 8 of 376 files: when present `Address` is absolute-in-image and the
offset is `Address - ImageBase`; when absent it is already image-relative.

### Verified against the entire library, on the console
Every one of the **376 files / 1567 patches / 19,773 lines** was parsed by the console and compared
against an independently written reference:

    errors 0   mismatches 0   applicable 19,223   unsupported 550   total 19,773

**550 lines (18% of patches) use mask signatures and are reported as unsupported rather than quietly
dropped** — a patch that can only be half-applied says so instead of looking whole.

### Two real bugs the testing caught
**`xml_attr` scans forward without a bound**, so a patch inherited the *next* patch's attributes. On
hardware the first patch of a two-patch file picked up the second one's `ImageBase`, every address
then compared as out of range, and a perfectly good patch reported `lines=0`. Attribute lookups are
now bound to the opening tag.

**`PATCH_MAX_VAL` was 256, and one line needs 970.** The first survey measured the length of the
*Value text*; a `utf8` line in CUSA13233 expands to 970 bytes when encoded — 8x the "obvious"
reading of the same corpus. Caps are now measured by encoding every line, not by eyeballing.

### Safety — a patch has no "off" bytes, so the version IS the check
A cheat is gated on memory already holding the documented opposite bytes. A patch has no such
baseline, so instead: the game must be the one running, **`AppVer` must match the installed version**,
every address is range-checked, the **original bytes are saved before writing** so a patch can be
undone, and every write is read back and compared.

Dark Souls Remastered is exactly why the version gate exists: the library holds a "Restore Debug
Camera" patch built for **01.03** while **01.00** is installed. It is listed, described, and refused.

### Fixed — the panel forced the one case the check exists to stop
The Apply button sent `force:!info.compatible` — forcing *precisely when the versions did not match*.
It never forces now. Rows show what a patch does, who wrote it, which version it targets and how many
edits it makes, and the button explains in words why it is unavailable.

Also: a title with patches but **no** cheat file used to return early with `patches:[]` and show
"No cheat file for this title" — Dark Souls is exactly that case. Both engines now report its patch.

### Unchanged
Downloads, mount/install and the PS4 PKG path were not touched. Cheat toggling is unaffected; the POST
`apply` action, previously a silent alias for `toggle`, now means "apply a patch" — which is what the
only caller in the UI always meant by it.

## [3.13.0] — 2026-07-31 · "The Mutant Cheat Engine was never broken — the PC just refused to ask it"

### Fixed — "CheatRunner is not running on the PS5 (:9999)" when toggling a mod
The engine was fine. Toggling a mod from the PC went through `POST /api/mods/<tid>/toggle`, and that
handler opened with:

    cr = b.cheats
    if not cr.alive():
        return 503 cheatrunner_offline

That gate ran **before anything else**, for all four actions. Twenty lines below it sat a complete
implementation on our own engine (`mutant_running` + `mutant_apply`) that never touched CheatRunner —
and was unreachable, because the gate returned first. So the mods panel could **read** live on/off
state through our engine on the GET, and then refuse to **change** it on the POST, blaming a service
this project deliberately removed.

Proven side by side against DOOM Eternal (PPSA01981) running at pid 183:

| call | before |
|---|---|
| `POST 10.0.0.99:8710/api/mods/PPSA01981/toggle` (console ELF) | `ok:true … entries=1 written=1 skipped=0 failed=0` |
| `POST 10.0.0.76:8710/api/mods/PPSA01981/toggle` (PC companion) | `ok:false, "CheatRunner is not running on the PS5 (:9999)."` |

The console's own ELF already implemented **all four** actions — select, toggle, apply, disable-all —
with expect-gated writes and the on-screen toast. The companion now asks it, through one new method
`Ps5Bridge.mods_action()`, instead of keeping a second copy of the engine that could drift. Our engine
goes first, exactly like the GET path; CheatRunner is finally what the comment above it always claimed
it was — a last resort, never a requirement. It is consulted only when our engine reports it has no
cheat file for the title *and* CheatRunner happens to be running.

### Verified live, in-game
DOOM Eternal running, all four mods driven **from the PC**:

    toggle Inf Ammo            ON   -> entries=2 written=2 skipped=0 failed=0
    toggle Inf Hp And Armor    ON   -> entries=1 written=1 skipped=0 failed=0
    toggle Inf Jumps           ON   -> entries=1 written=1 skipped=0 failed=0
    toggle Inf Upgrade Points  ON   -> entries=2 written=2 skipped=0 failed=0
    toggle Inf Jumps           OFF  -> entries=1 written=1 skipped=0 failed=0
    disable-all                     -> disabled=4 failed=0
    re-enable all four              -> 6 entries written, 0 failed

State read back from the game's live memory after every step, and the ELF stayed healthy throughout.

### Fixed — the mods panel reported success it had not checked
`Disable all mods` toasted **"All mods disabled"** unconditionally: it never looked at the response.
Now that the engine can legitimately answer `game_not_running`, that would have been a straight lie.
It now reports the refusal, and on success says how many mods it actually reverted.

A failed toggle also showed only the error *code*. Both write paths now prefer the engine's own
sentence — "Launch the game first — cheats are written into its live memory", or its refusal to write
because the game's code does not match the cheat file — which is the part that tells you what to do.

### Unchanged
Download, mount/install and PS4 PKG paths were not touched. The engine's safety model is exactly as
it was: every entry is gated on memory already holding the documented opposite bytes, mismatches are
refused rather than forced, and `force` stays opt-in.

## [3.12.0] — 2026-07-31 · "PS5 games install. It was one setting."

### Solved — PS5 backups now register, with no console restart
Last night's conclusion was that the console had to be rebooted. **That was wrong**, and this is the
correction. The whole failure came down to a single ShadowMount setting.

ShadowMount does two separate things to a backup: it **mounts** the container, then it **registers**
the title. Mounting always worked. Registering always failed:

    [REG] internal AppInstallTitleDir bridge unavailable
    NOTIFY: Register failed: Evergate (PPSA01885)

That "internal bridge" is a patch ShadowMount writes into SceShellCore — and on this console it is
never installed at all. Its own startup log says so, every single boot:

    [SHELLCORE] unexpected prologue: launchApp at 0x1563c660
    [SHELLCORE] lifecycle hooks unavailable; stock behavior kept

So the direct per-title register route **cannot** work here. ShadowMount has a second route, and its
own documentation says this firmware is supposed to be on it already:

    # Use batch registration for newly staged titles:
    # 1/true/yes/on  -> submit queued installs through sceAppInstUtilAppInstallAll
    # 0/false/no/off -> register each title directly
    # Default: 0 on FW below 12.00, forced to 1 on FW 12.00 and newer

The console is on **12.70** — but its runtime config dump read `app_install_all=0`. That "forced to 1"
does not happen in 1.7alpha4, and the key was not in the config file, so ShadowMount used the broken
route on a firmware where it can never succeed.

Setting `app_install_all=1` and restarting ShadowMount, against the **same file already sitting on the
console**:

    [REG] Prepared for batch install: Evergate (PPSA01885)
    [REG] Batch install request contains 1 title(s):
    NOTIFY: Batch install queued (1):
    [REG] Installed: Evergate (PPSA01885)
    NOTIFY: installed game PPSA01885

`/user/appmeta/PPSA01885` came out complete — `param.json`, `icon0.png`, `pic0/1/2` — which is exactly
what was missing when the PS5's own notification said **"Unknown"** with no artwork.

No reboot. No jailbreak lost. One line of config.

### The app now fixes this itself, before it costs you an hour
A mount-lane transfer checks the console's register route **before** sending a byte, and repairs the
config if it is wrong. It only rewrites after reading the file in full, preserves the original text
byte for byte, and appends the key rather than reformatting — the fakelib excludes, kstuff rules and
scan paths in that file are yours and are left alone. Once correct it is a no-op.

### Fixed — "ready to play" for a game that was not there
`title_is_mounted()` now requires **both** signals, because each one alone has already lied:

| signal | how it lied |
|---|---|
| `mount.lnk` alone | written *before* the register attempt — passed for Evergate, which the console had never registered |
| app.db alone | a registration outlives its data — Little Nightmares III sits in app.db at 75 MB of metadata with **no container on the console at all** |

Little Nightmares III is the clean proof: `/user/app/PPSA05144` holds only `icon0.png` and `sce_sys`
— no `mount.lnk` — while app.db still lists it. Waiting on app.db alone would have passed instantly,
on a tile that crashes. Checked against every title on the console:

    17 working games   in app.db + mount.lnk  -> ready
    PPSA05144          in app.db, no mount.lnk -> correctly rejected

18/18 correct.

### Unchanged
The PS4 PKG path through Elf Arsenal DPI v2, the Mutant Cheat Engine, downloads, patches and mods are
untouched. Verified after the change: 17/17 PS5 games still mounted, PS4 titles' `app.pkg` intact.

## [3.11.0] — 2026-07-31 · "The PS5 side is a console fault, and the app now says so"

### What is actually wrong — proven, not guessed
PS5 backups reach the console perfectly and still do not work. ShadowMount's own log says why:

    [ACTION] Installing: Evergate (PPSA01885)
    NOTIFY:  Installing: Evergate (PPSA01885)...
    [LINK]   nullfs mounted: /mnt/shadowmnt/PPSA01885_... -> /system_ex/app/PPSA01885
    [LINK]   mount.lnk created / mount_img.lnk created ... layers=2
    [REG]    internal AppInstallTitleDir bridge unavailable
    NOTIFY:  Register failed: Evergate (PPSA01885)

The container mounts — **both layers, pfsc and the inner exfat** — and the links are written. Only the
final **register** step fails, because the console's `AppInstallTitleDir` bridge is unavailable. A
title that mounts but never registers is exactly the broken tile that crashes on launch.

**It is not this app.** Tested five ways, all with the same result:

| test | register bridge |
|---|---|
| our ELF stopped completely, ShadowMount reloaded | unavailable |
| Elf Arsenal reloaded (fresh `dpiv2`), ShadowMount restarted | unavailable |
| DPI processes stopped, ShadowMount restarted | unavailable |
| `tile-autoinst` **and** DPI stopped, ShadowMount started first | unavailable |
| everything running as normal | unavailable |

And the delivery itself is provably correct: content **byte-identical** (SHA-256 over five 8 MB chunks
including head and tail), permissions and ownership identical to the 16 backups that do work
(`-rwxrwxrwx 0 0`), correct folder, correct size.

The bridge works at boot — which is why all 16 existing games mount at startup — and goes down during
a session. **Only a console restart clears it.**

### Fixed — the app claimed "Deployed" for a game the console had rejected
The mount lane reported success as soon as the file was delivered. Delivery is not the finish line;
ShadowMount still has to mount *and register*. It now waits for the title to appear in the console's
`app.db` — the thing that only follows a successful register — and if that never happens it says:

> Copied to /mnt/ext1/homebrew, but the PS5 did not finish mounting it. ShadowMount can mount the
> image and still fail to register the title — restart the console, then it will pick it up from the
> same file.

A first attempt at this checked for `mount.lnk` and was **wrong**: the log above shows `mount.lnk` is
written *before* the register attempt, so it passed for a title the console never registered. The
check now uses `app.db`, verified against the failing case.

### Established — Elf Arsenal cannot install a PS5 backup
Handing Arsenal a `.ffpfsc` exactly the way a PS4 PKG is handed over:

    POST 10.0.0.99:12800/api/install {"url": ".../[PS5] PPSA01885 - Evergate.ffpfsc"}
    -> {"res":"-1","error":"install failed"}

Its own UI confirms it: **`.pkg` is the only extension anywhere in Arsenal's 500 KB `app.js`** — no
`.ffpfsc`, `.ffpfs`, `.ffpkg` or `.exfat`. Its installer is PKG-only.

This also explains the remembered "Download/Install Started" toast for a PS5 game: **before 3.6.1 a
backup held on another PC was forced onto lane `install` and submitted to the DPI daemon.** That
produced Arsenal's toast — and then failed, because Arsenal cannot install a backup. The message was
real; the install never was. FTP to `/mnt/<drive>/homebrew` has always been the correct route, and it
is what the app does.

### Unchanged and re-verified — the PS4 path
Re-tested after every payload restart in this session: Castle Crashers 0.23 GB through Elf Arsenal
DPI v2 → `app.pkg` **227,540,992 bytes = source, byte-identical**, full `app.pbm`/`app.json`/`app.xml`
set, registered in `app.db`. Untouched by any of the above.

### Console left as found
All payloads restored to the same set and all **16/16** PS5 games still mounted: Elf Arsenal
(`dpiv2`, `dpi`, `ftpsrv`, `nanodns`), `shadowmountplus`, `tile-autoinst`, `garlic-savemgr`,
`kstuff`, `pldmgr`, `elfldr`, and our own ELF. DPI live, FTP up.

### What to do next
**Restart the console, then transfer a PS5 game before doing any PKG install.** If it registers, the
bridge is confirmed as session-lifetime state and the app can warn before wasting a big transfer.
The Evergate container is already in `/mnt/ext1/homebrew`, so it should be picked up on boot with no
further transfer.

## [3.10.1] — 2026-07-30 · "ShadowMount never sees a half-written game again"

### Fixed — PS5 backups mounted while they were still uploading
This is why PS5 games installed "successfully" and then crashed the console, and why their
notification said **Unknown** with no artwork.

ShadowMountPlus is an **auto-mounter**: it watches its scan folder and mounts whatever appears —
including a file still being written. The upload wrote straight to the final `.ffpfsc` name, so the
mounter could grab the container at 30% and mount an incomplete image. The metadata is not readable
yet, hence "Unknown" and no icon; the game then crashes on launch because most of it is not there.
The transfer would finish afterwards and the file would look perfect, which is why nothing seemed
wrong when checked later.

Uploads now go to `<name>.part` and are renamed only once every byte is across. The mounter sees
nothing until the file is complete and correct. Verified live, watching both sides during a
transfer:

    transferring   0%   scanfolder=Evergate.ffpfsc.part   mounted=no
    transferring  42%   scanfolder=Evergate.ffpfsc.part   mounted=no
    transferring  84%   scanfolder=Evergate.ffpfsc.part   mounted=no
    playable     100%   scanfolder=Evergate.ffpfsc        mounted=no
    playable     100%   scanfolder=Evergate.ffpfsc        mounted=[sce_sys, icon0.png, mount.lnk, mount_img.lnk]

The rename also clears any stale copy first, and `RNFR`/`RNTO`/`DELE` were probed on the console's
FTP before relying on them.

### Fixed — large PS5 transfers died partway
Little Nightmares III (17.5 GB) failed at **23%** — 4,066,902,016 bytes — with "FTP push failed:
timed out". `ftp.connect(timeout=5)` and **ftplib gives the data socket that same timeout**, so any
stall longer than five seconds killed the transfer. The console pauses for exactly that while it
flushes gigabytes to extended storage. A 2 GB game happened to squeak through; a 17.5 GB one never
could — which is why only the big titles failed. The data timeout is now 900s.

### Verified end to end on hardware

| | transfer | mount | appmeta |
|---|---|---|---|
| **Evergate** (local, 1.98 GB) | 1,983,840,256 = local, byte-identical | `mount.lnk`, `mount_img.lnk`, `sce_sys`, `icon0.png` | full |
| **Little Nightmares III** (on Casita, 17.5 GB, delegated) | 17,484,021,760 = source, byte-identical | `mount.lnk`, `mount_img.lnk`, `sce_sys`, `icon0.png` | full |
| **Castle Crashers** (PS4 PKG → DPI v2) | `app.pkg` 227,540,992 = source | — | registered, patched to 01.04 |

No `.part` leftovers, both queues empty, DPI still `live` after the run, all three machines on 3.10.1.

`appmeta` being populated is what gives the PS5 notification the real game name and artwork — the
"Unknown" toast was the symptom of mounting before it existed.

## [3.10.0] — 2026-07-30 · "It only says installed when the game is actually there"

### Fixed — the app reported "Ready to play" for an install that never happened
Castle Crashers showed as installed and then crashed the console on launch. The install itself was
routed correctly; **the confirmation was lying.**

A finished install was decided by one thing: the title appearing in the console's `app.db`. That is
sound for a game the console did not have — but it is worthless for one it already had. As recorded
back in July, a title that is **already registered** makes the installer skip the download entirely
and keep the existing files: it reads the package header, sees the title registered, and refuses to
re-pull. Our check then found the id in `app.db` — put there by the *old* install — and called it
ready. A broken tile went on the dashboard, and launching it took the console down.

Confirmation is now evidence-based:

* **The state before the handoff is recorded.** If the title was already registered, its presence in
  `app.db` afterwards proves nothing and is no longer accepted on its own.
* **A reinstall must show its data on disk.** `installed_app_pkg()` reads the real
  `/user/app/<TID>/app.pkg` (or `/mnt/ext<n>/user/app/<TID>/app.pkg`) over FTP and requires it to be
  at least 98% of the package size. Metadata without data — the exact shape of the tile that crashes
  — can no longer pass.
* **A first-time install is also checked**, but only to catch a *visibly short* file (<90%). If the
  path cannot be read at all the result is left alone, so nothing that works today starts failing.
* **The dead end is named.** When a leftover blocks the re-pull the message is now
  *"Already installed on the PS5, so it was not replaced — the old copy is still there and may be
  broken. Delete it on the console (Options → Delete), then install again."* — the documented remedy,
  instead of a false success.

### Fixed — the repo config pointed installs at the wrong port
`config.json` in the source tree had `consoles[].dpi_port: 9040` (the ezremote port) while
`dpi.mode` is `v2` (12800). Running from source would have handed every install to a port with
nothing listening. The deployed copy was already correct; both now agree.

### Audited — the whole download/install path, end to end
Traced rather than assumed, in order:

| lane | condition | destination |
|---|---|---|
| **MOUNT** | local backup container | FTP → `/mnt/<drive>/homebrew` → ShadowMount mounts it |
| **CONSOLE-LOCAL** | package already on the PS5 | served over HTTP → DPI v2 |
| **PEER** | game on another PC | that PC's URL as the source, or the mount delegated to it |
| **INSTALL** | PKG in the library | served on the LAN → `POST :12800/api/install` → **Elf Arsenal DPI v2** |

`dpi.mode = v2` in the live config, so every PKG goes to Elf Arsenal. The console's own local install
hands off through `dpi_install_url()` — **not** `sceAppInstUtilAppInstallPkg`, which only writes
metadata and is what produces an unplayable tile.

The self-healing around the handoff is intact and unchanged: `ensure_dpi_host` brings the host up on
demand, a host marked dirty by the previous install is reloaded before the next one
(`recover_dpi`), `ensure_dpi_ready` runs before every hand-off, a wedged daemon is reloaded once and
the task re-driven, and if that fails the queue pauses and says so.

### Still to verify on hardware
The console is being re-jailbroken, so none of this could be tested live. What was verified statically:
every lane's routing, the DPI port and mode, the confirmation logic, and that both artifacts build.

**Castle Crashers is almost certainly still broken on the console.** Delete it there
(Options → Delete) before reinstalling — with a leftover in place the console will not re-pull it,
which is the very situation this release stops misreporting.

## [3.9.5] — 2026-07-30 · "Start means start, from any device"

### Fixed — "Waiting in Casita's queue — start it there"
That prompt should never have existed and was not asked for. It came from a flaw in how 3.9.0
delegated a peer backup: the other PC was contacted **when the game was queued**, and this queue's
mode was passed along with it. So adding a game with "+ Queue" created a *held* job on the other
machine, and pressing Start here released only the local mirror — leaving the app telling you to go
and start it on the other PC.

Delegation now happens when the task **runs**, not when it is queued, and the other PC is always
asked with `mode: "now"`. Queue behaviour stays entirely local: your "+ Queue" holds it here, Start
releases it here, and everything else follows automatically. Which device you press the button on
never matters again.

Two more consequences of the old design, also fixed:

* **No orphans.** Because nothing is sent until the task runs, cancelling a queued game leaves
  nothing behind on the other machine. (One such orphan — a held *Demon's Souls* — was found stranded
  on Casita from the previous behaviour and cleared.)
* **A held job over there is released, not reported.** If the other PC's queue ever holds the job for
  its own reasons, it is started rather than turned into a message you have to act on.

The fallback got better too: if the other PC cannot take it (asleep, busy, older build), this machine
now pulls the file and sends it on itself instead of stopping — slower, still fully automatic.

### Verified end to end, live
    queue a peer PS5 backup here   -> Casita's queue: EMPTY (nothing sent yet)
    press Start                    -> Casita job created and RUNNING immediately
    progress here                  -> "Uploading 2% (on Casita)"   — mirrored, no prompt
    cancel here                    -> "Canceled on Casita", both queues clean

The partial file the cancelled test left in `/mnt/ext1/homebrew` was removed, so ShadowMount has
nothing half-written to pick up.

## [3.9.4] — 2026-07-30 · "Installing from a PC works on the PS5 again"

### Fixed — the PS5 said "only packages already on the PS5 can be installed"
That message comes from the **console's own** `/api/install`, and seeing it means the app was talking
to the console instead of to a PC. The console cannot fetch a game off a PC, so every install died
there. Two faults, both mine, stacked:

**1. `/api/health` on a PC took 2.07s, and the console gives each candidate 1.5s.**
The whole 2 seconds was one dead call: `running_title()` still asked **CheatRunner** — removed from
this project long ago — and a refused connection costs ~2.07s on Windows. Our own engine already
answers that question in ~15ms. Because health was slower than the budget, the console skipped
*every* PC, found no companion, and fell back to its own API.

Its client swallows errors and returns a dict rather than raising, so the first attempt at a memo
never tripped — it now keys off the *result*. **Health: 2.07s → 0.004s.** The console's per-candidate
budget also went 1.5s → 4s, so a momentarily slow PC is not discarded.

**2. The console only ever offered ONE PC.** `/api/companion` returned `g_pc_url`, a legacy single
slot holding whichever machine announced itself last — even though the multi-PC registry has known
about all of them since 3.1.0. If that one machine happened to be asleep, installing was impossible
with other PCs wide awake. It now returns **every** known PC, most-recently-seen first, and the UI
tries them in order.

The app also re-checks every 20s while it is running on its own API, so a PC that wakes up after the
page loaded is picked up without a reload.

### Verified end to end, by installing — not by reading code

Simulating exactly what the PS5 does — page served by the console, ask for companions, try each
inside the UI budget, then install through the winner:

    STEP 1  on_console = True
    STEP 2  candidates: ['http://10.0.0.76:8710', 'http://10.0.0.72:8710']
    STEP 3  -> 10.0.0.76 OK in 0.048s   (the other one was down; it fell through)
    STEP 4  PS4 PKG   -> lane=install, source=companion-lan  (goes to Elf Arsenal / DPI v2)
    STEP 5  PS5 backup-> lane=mount,   dest=/mnt/usb3/homebrew  (the drive that was picked)

And with real transfers earlier in this session:

* **PS4 PKG** — Castle Crashers base, 0.23 GB: downloaded, handed to DPI v2, **"Ready to play"**,
  console installed count 23 → 24.
* **PS5 backup** — Evergate `.ffpfsc`, 1.98 GB: uploaded to `/mnt/ext1/homebrew`,
  **"Deployed — ShadowMount picks it up on the next scan"**.
* **On another PC** — routes to that PC's URL as the source (`source: peer`), or delegates the mount
  to the machine holding the file.

The DPI self-healing between installs is untouched and still in place: `ensure_dpi_host` on demand,
proactive `recover_dpi` for a host marked dirty, then `ensure_dpi_ready` before every handoff, with
the queue pausing and saying so if a reload genuinely fails.

### Note on "base_not_installed"
Seen twice while testing and correct both times — the base really had been uninstalled on the console
between attempts. It is not related to any of the above.

## [3.9.1] — 2026-07-30 · "The drive picker is a destination list again"

### Fixed — the install destination list lost the USB drives
It came from 3.4.0. The storage bar was changed to show only what is *actually holding
games* — which was right for the bar, but the drive picker used that same list as its fallback. Two
different questions, one list:

* **the bar** answers "what is holding games right now" — and since 3.4.0 it also lists the PCs
* **the picker** answers "where on the console can this go"

So when the fallback was used the picker offered whatever happened to be populated — internal and
extended only — and could even offer **another PC** as an install destination. Every USB was gone.

The picker no longer looks at the bar at all. `ps5Destinations()` returns the fixed set of console
destinations (internal, ext0, ext1, usb0–usb7) and merges in real free space and labels when the
console reports them. Verified: a PS5 backup panel lists **all 11 destinations** again, and choosing
`usb0` produces `dest: /mnt/usb0/homebrew`.

The console's `/api/devices` now reports every destination too, each flagged `detected`, instead of
only the ones currently mounted — it was offering two choices where the PCs offered eleven, so a
stick you were about to plug in could not be selected at all. On the console those show as
"not detected" so the choice is informed; on a PC the list behaves exactly as it always did.

### Verified — the download and install system is intact, end to end, with real transfers
Because the report was that it was broken, it was tested by actually installing, not by reading code:

| path | result |
|---|---|
| **PS4 PKG, local** — Castle Crashers base, 0.23 GB | downloaded and installed, **"Ready to play"**; console installed count 23 → 24 |
| **PS5 backup, local** — Evergate `.ffpfsc`, 1.98 GB | uploaded to `/mnt/ext1/homebrew`, **"Deployed — ShadowMount picks it up"** |
| **PS4 PKG, on another PC** | queues `lane=install` with that PC's URL as the source |
| **PS5 backup, on another PC** | delegates to the owner, which runs its own mount lane |

One thing that looked like a bug and was not: installing the Castle Crashers *update* was refused with
`base_not_installed`. That was correct — the base had been uninstalled during testing. Once the base
was installed the refusal stopped.

### Also fixed — the actions (⋯) menu
Covered in 3.9.0 but worth restating, since "it does nothing" is what a menu drawn off-screen looks
like: it is now placed by measurement and moved to `<body>`, because the drawer's `transform` was
making a `position:fixed` child resolve against the panel instead of the viewport.

## [3.9.0] — 2026-07-30 · "A PS5 game goes to the PS5, not to the other PC"

### Fixed — installing a peer backup copied it between PCs instead of sending it to the console
Asking for Demon's Souls on the internal SSD moved it from Casita to ASUS-LAP. The mount system was
never broken — a **local** backup still takes the working MOUNT lane, and that path is untouched —
but 3.7.0's `peer-mount` lane pulled a remote container onto this PC first and only then pushed it to
the console. Two transfers of a 100GB file, and the visible half was the wrong one.

The PC that **owns** the file can send it to the console itself, so it does. The install request is now
handed to that machine, which runs its own already-working mount lane straight into
`/mnt/<drive>/homebrew` on the drive you picked. One transfer, machine to machine, never through the
PC you happen to be sitting at.

Progress is mirrored locally by a `peer-delegate` task, so you watch it finish in the app you started
it from instead of having to open the other PC's. Cancelling here cancels it there. If the owner is
unreachable or on an older build, it falls back to the previous copy-then-send rather than failing.

Verified with a held job so nothing moved: asking ASUS-LAP to install Elden Ring to the internal SSD
put `lane=mount, state=held, drive=internal` on **Casita**, with only a `peer-delegate` mirror here —
then both were cancelled and both queues came back empty with nothing transferred.

### Added — choose which PC a copy goes to
The PC-to-PC copy is the alternative action, and it always copied to whichever machine you had the app
open on. It now asks which PC should end up with it, listing every online peer except the one that
already has it, and hands the job to that machine to run. `/api/transfer` takes a `to` and relays.

### Fixed — the actions (⋯) menu opened off the top of the screen
Same root cause as the hover tips: it was anchored to its button and clipped by the panel, with no way
for CSS to know it was about to leave the screen. It now places itself by measurement — above the
button by preference, below when there is no room, clamped horizontally.

One extra trap found on the way: the drawer animates with `transform: matrix(0.975, …)`, and **any**
transform on an ancestor makes a `position:fixed` child resolve against that ancestor rather than the
viewport — so a fixed menu inside the panel still landed in the wrong place. The menu is moved to
`<body>` when opened, where nothing can capture it. Verified at five extreme button positions
(each corner and centre): **zero off-screen**, and in the real panel it sits above the button, right
edges aligned.

## [3.8.0] — 2026-07-30 · "Helpers wait for the console to finish waking up"

### Fixed — waking from rest crashed intermittently
Stopping the homebrew before rest made suspend reliable, but the PS5 auto-starts payloads again on
wake — and that is where it was still crashing, sometimes.

The cause was timing, not any single payload. On wake the whole chain comes up at once, and our
bootstrap asked Payload Manager to load two more payloads **the instant our process began** — while
the shell, the network stack and Payload Manager itself were still assembling. Sometimes the system
was far enough along to survive it; sometimes it was not. That is exactly the shape of a fault that
happens "sometimes".

The helper bootstrap now waits for the console to actually be ready. **Nothing about what gets
launched, or in what order, changed — only when.** Three gates, each individually bounded so a
console that never reports itself ready still ends up with its helpers running:

1. **A delay you can set** — default **30s**, adjustable 0–600s.
2. **Payload Manager answering `/version`** (up to 90s). We load *through* it, so launching before it
   is up could never have worked anyway.
3. **A user signed in** (up to 90s), via `sceUserServiceGetLoginUserIdList` — the closest thing the
   system offers to "the PS5 has finished starting".

Then a short settle, and the existing launch logic runs untouched. The autostart switch is re-checked
after the wait, since it is now long enough that you may have turned it off in the meantime.

### Added — Settings › Network › "Start helpers: after …"
Immediately / 15s / 30s / 45s / 60s / 90s / 2 min, alongside the Helpers toggle, and hidden when the
helpers are not set to auto-start at all. Works from the PC, the PS5 or a phone — the companion relay
now forwards the whole query string rather than just `on`, which would otherwise have dropped the new
setting silently.

Verified live on the console: read `30`, set `60`, `9999` clamped to `600`, restored to `30`, and the
same through the PC relay and through the UI control itself. The gated bootstrap then reported
`shadowmount=already-running, arsenal=already-running` — it waited, checked, and correctly relaunched
nothing.

### Checked — the rest-mode stop list against the console's real process names
With the console up, Payload Manager's own process list was read and every name cross-checked against
the allow-list. All six homebrew helpers match under their real names — `garlic-savemgr`, `dpi.elf`,
`dpiv2.elf`, `nanodns.elf`, `ftpsrv.elf`, `shadowmountplus.elf` — and, most importantly,
**`kstuff.elf` is left alone**: that is the jailbreak itself, and stopping it would be far worse than
the crash being chased. `klogsrv` and `tile-autoinst` are also left running; rest mode is already
reliable without touching them, so they stay.

Also confirmed our own process appears as `payload.elf` (pid 323 this boot) — the sweep skips it by
pid and exits last, rather than by name.

## [3.7.1] — 2026-07-29 · "The console can pick the drive too"

### Fixed — the drive picker worked on the PCs but not on the PS5
3.7.0 gave the mount lane to peer backups in the *companion* merge only; the console's own merge still
hardcoded `lane:"install"`, and the picker is gated on the mount lane. So a `.ffpfs` on Casita offered
all 11 destinations from a PC and only *"Installs to: console default"* on the console.

The console's merge now takes the lane from the container type, the same way the companion does —
verified live: `Elden Ring lane=mount`, `Marvel's Spider-Man 2 lane=mount`, while PKG titles stay
`lane=install`.

### Fixed — non-ASCII in a title arrived as literal escape text
`Star Wars™: Racer Revenge™` displayed on the console as **`Star Warsu2122: Racer Revengeu2122`**.
Python's json writes that ™ as `\u2122`; `json_str_after()` dropped the backslash without decoding the
escape, leaving the four hex digits behind as text. It now decodes `\uXXXX` to UTF-8 (and `\n`, `\t`,
`\"`, `\\`, `\/` properly), and a malformed `\u` is swallowed whole instead of leaking its digits.

`json_escape()` was the other half: it dropped **every** byte >= 0x80, so real characters were stripped
out of real titles along with the mojibake it was aiming at. It now passes valid UTF-8 through and drops
only genuinely broken bytes — so `Bloodborne™` keeps its ™, while invalid bytes still disappear.

Both functions also parse cheat mod files, so both changes were proved on the PC first: **21/21 cases**,
covering escaped quotes in mod names, Windows paths, truncated sequences and lone continuation bytes.

### Added — artwork is fetched the moment a backup lands
A backup container carries no icon; the console only creates `/user/appmeta/<id>/icon0.png` once the
title is actually there. After a successful install/mount the companion now watches for that icon for a
minute and rescans as soon as it appears, so the card picks up its real art straight away instead of
keeping its initials until something else happens to trigger a fetch. Once cached it is advertised to
every other device as `icon_url`, exactly as for any other title.

### Why Elden Ring shows "ER" until you mount it
The sharing pipeline is already complete, and was verified end to end: the PC pulls artwork from the
console's appmeta over FTP (a real 304KB PNG for Evergate), advertises `icon_url` to peers, and the
console picks peer artwork up the same way. What is missing is the *source* — `PPSA04610` has never been
on the console, so no `icon0.png` exists on any device, and the artwork inside the container is not
reachable: the first 4MB of that 109GB `.ffpfs` is a sparse PFS superblock, 99.9% zeros, with no
filenames and no PNG data in it. Mount it once and the icon, version and region all fill in by
themselves — nothing else is needed.

## [3.7.0] — 2026-07-29 · "Every backup container behaves like .ffpfsc"

### Fixed — a backup on another PC had no drive picker
`.ffpfsc` was never actually privileged in the scanner: local backups all share one `MOUNT_EXTS` path
with no per-format branching, so a local `.ffpfs` or `.exfat` already behaved identically. The real
split was **local vs remote** — the peer merge forced `lane:"install"` on everything, and the drive
picker is gated on `lane === "mount"`. That is why Elden Ring offered only *"Installs to: console
default"*.

A backup is a backup wherever it sits, so it now keeps the mount lane and with it the picker. Detection
(`is_backup_item`) checks the kind, the format from either side, **and** the filename — so it holds for
a peer on an older build that reports none of them. 19/19 cases verified: every container in
`MOUNT_EXTS` (`.ffpfsc .ffpfs .ffpkg .ffpfsx .fpkg .exfat .iso .img`) is treated alike, and no PKG is
mistaken for one.

Verified side by side — Elden Ring (`.ffpfs`, on Casita) now matches Evergate (`.ffpfsc`, local):
**picker enabled with all 11 destinations**, button reads **Mount backup**, format reads **FFPFS**.

### Added — the peer→mount lane, so that picker does something
The action had to exist before offering it. A remote backup cannot go through the PKG installer, and
the mount lane needs the file locally. The new `peer-mount` lane runs the two proven halves in order:
pull the container into this library exactly as the PC-to-PC copy does, then send it to
`/mnt/<drive>/homebrew` exactly as a local backup does. **Neither `_run_pc_copy` nor `_run_mount` was
modified** — a failure in either reports itself and stops.

### Added — games stored as a FOLDER
An unpacked dump is now listed like any other backup: `format: folder`, mount lane, drive picker.
Recognised by structure (`eboot.bin` / `sce_sys` / `param.json` / `param.sfo` / `sce_module`), never by
name, so ordinary directories are left alone; a claimed folder is pruned from the walk so its contents
do not come back as loose packages; and its size is the sum of its contents rather than the directory
entry. Sending one recreates the tree over FTP — added as its own branch so the single-file push every
container has always used is untouched. Verified on a synthetic tree: the game folder found and named,
`sce_sys`, `random` and `NotAGame` all correctly ignored.

Peer transfer of a folder is *not* included: `/library/<key>` streams a single file, and a folder would
need an archive stream. Local folders work fully.

### Fixed — the console labelled every backup "ffpfsc"
`build_library_json()` hardcoded that string, so an installed title backed by an `.ffpfs` or `.exfat`
described itself as the wrong container. It now reports the real extension.

### Not a bug — Elden Ring has no artwork, version or region
Worth stating plainly since it looks like one. Artwork for a backup is fetched from the console's
`/user/appmeta/<title id>/icon0.png`, and version and region come from `app.db` — **both exist only
once a title has been installed**. Evergate has all three because it *is* installed (`PPSA01885` is in
app.db); Elden Ring is not (`PPSA04610` absent), and an `.ffpfs` container carries no extractable
metadata of its own. Nothing is being dropped — there is no source yet. Mount it once and the icon,
version and region fill in by themselves.

### Unchanged
PS4 packages still install to the console default with the picker locked, exactly as before —
re-verified on Castle Crashers and Bloodborne after these changes.

## [3.6.1] — 2026-07-29 · "Backup formats read properly, and a badge that fell out of its corner"

### Fixed — a badge landed at the bottom-left of the cover, sliced in half
`.tip{position:relative}` sits later in the stylesheet than `.badge{position:absolute}` and has the
**same specificity**, so it silently won. Any badge that also carried a tip lost its absolute
positioning, flowed as inline content inside the cover, and was clipped at the bottom edge. That is
exactly the three that were affected — *Patch only* on add-ons, the source-PC name (**Casita** on the
Elden Ring card), and the drive name on removable media. Every other badge was fine because it has no
tip.

`.tip` existed only to anchor the old `:after` tooltip. Since 3.6.0 the tooltip is a fixed-position
element on `<body>`, so the rule was already dead weight — removing it fixes the badge outright rather
than papering over it with a higher-specificity override.

### Removed — the "Patch only" tag on add-on cards
It sits under **Add-ons** already and the panel spells out that the base game is missing, so the tag
said nothing the card did not. Add-on cards now show platform, id, size and name — nothing else.

### Fixed — ShadowMount backups arrived from another PC with no information
A `.ffpfs` held on another PC showed **FORMAT —** and no version, because neither field ever crossed
the network: `federation_self` did not advertise `format`, and the merge did not read it. Both ends
now carry the container type (per title *and* per file) and the version, so a backup on Casita reads
as **FFPFS** on every device instead of an anonymous package. Same fix in the console's own merge,
which was hardcoding `"format":"PKG"` for every peer title.

`lane` is deliberately **not** taken from the peer. It selects the action the panel offers, and
pulling a backup across the network is a different job from mounting one that is already local —
changing it was not needed to fix the missing information.

### Fixed — a JSON null could be read back as the next field's name
Found while wiring the version above. `json_str_after()` stepped past the key and then scanned ahead
for the next quote, so `"version":null,"items":[...]` returned **"items"** as the version. It now steps
over exactly `:` and requires a quoted string, giving nothing for null / numbers / objects / arrays.

This function is also what the **cheat engine** parses mod files with, so the change was proved on the
PC first: 13/13 cases, including escaped quotes inside names (`Inf \"HP\"`) and the exact document a
peer sends. Every previously-correct read is unchanged — only null-valued fields differ, and returning
nothing there is strictly better than returning another key's name.

### Fixed — backup names carried their own plumbing
A backup container has no `param.sfo`, so its card name is its filename — which read
*"Elden Ring - PPSA04610-app"*, repeating the id that already has its own field. Platform tags,
version tags in brackets, the title id and a trailing `-app` / `-patch` marker are now stripped:

    Elden Ring - PPSA04610-app.ffpfs        ->  Elden Ring
    [PS5] PPSA01885 - Evergate.ffpfsc       ->  Evergate
    DOOM Eternal - PPSA01981-app.exfat      ->  DOOM Eternal

The marker strip is anchored to the end **and** requires the hyphen the backup tools actually write.
An earlier looser version turned *"The Last Game"* into *"The Last"* — 11/11 cases correct now,
including titles that genuinely end in "Game" and files that are nothing but an id.

### Fixed — three different lists of which formats count as a backup
The library scanner accepted eight extensions, the console accepted three, and the console-backup
scanner four. A stick holding an `.exfat`, `.iso`, `.ffpfsx` or `.fpkg` backup was therefore listed by
one scan and ignored by the next. All three now use one list — `.ffpfsc .ffpfs .ffpkg .ffpfsx .fpkg
.exfat .iso .img` — via a single `is_backup_ext()` on the console side.

### Known limitation, unchanged by this release
Installing a **backup that lives on another PC** is not wired: a `.ffpfs` cannot go through the PKG
installer, and the mount lane requires the file to be on this machine. The card and panel now describe
it correctly; the action does not yet exist. Copy it over with the PC-to-PC move first, then mount it.

## [3.6.0] — 2026-07-29 · "Tips that stay on screen, and cleaner game cards"

### Fixed — hover tips were being cut off
A tip on a header button was drawn *above* it, which on the console put it behind the browser's own
top bar; tips inside the game panel and the Settings blocks were sliced by the panel edge.

The cause was structural: the tip was a CSS `:after` pseudo-element on each button. A pseudo-element
is **clipped by any ancestor that scrolls or hides overflow**, and CSS cannot know a tip is about to
run off the screen — so no amount of tweaking `top`/`right` could fix it everywhere. There was
already a per-panel override trying to work around exactly this for one case.

Replaced with a single fixed-position element on `<body>`, placed by measurement:

- **Prefers above**, and **flips below** when there is no room up there — which is what rescues the
  header buttons.
- **Clamps horizontally** so a control near either edge still shows its whole tip.
- **Fixed positioning escapes every container**, so no panel can clip it — the per-panel special
  case is gone rather than multiplied.
- Hides on scroll, resize and click, since a tip is anchored to where the control *was*.

No markup changed: it works by delegation on `data-tip`, so all 21 existing tips are covered and any
new one is automatic. Touch behaviour is unchanged (still suppressed, where hover never happens).

Verified at 1920×1020 across every visible tip on the main page, the whole Settings panel and an open
game panel: **14/14 fully on screen, zero clipped.** The two header buttons now place their tip
*below* (y=84 and y=82 instead of off-screen above); dock and Settings controls place theirs above.

### Changed — title id and size moved onto the artwork
They had a text row of their own under the game name, costing every card a line. They are now tags
along the bottom of the cover — id bottom-left, size bottom-right — in the **same style as the PS4 /
PS5 tag above**, inset by the same 8px, and **white** so they read against any cover.

The bottom text row is gone entirely. The card's text block drops from **73px to 51px** — 22px back
per card, across every card in the grid, add-ons included (they all come from the same builder).
The platform tag and the status badge are untouched: same position, same behaviour.

## [3.5.0] — 2026-07-29 · "Rest mode confirmed fixed, and a slimmer header"

### Confirmed on hardware — rest mode works
*Stop all homebrew before rest mode* was pressed, confirmed, and the console **suspended without a
panic**. The diagnosis holds: our app was never the cause — the other loaded payloads were. Elf
Arsenal (with the `dpiv2` / `nanoDNS` / `ftpsrv` processes it leaves behind), GarlicSaves and
ShadowMount are not part of the console's sleep/wake cycle, and stopping them makes suspend safe.

### Added — the shutdown is one press from the main page
A power icon sits beside the settings gear. It runs **exactly** the same action as the Settings
button — both call one shared function, so the shortcut can never drift from the original. The
Settings control is untouched.

It appears only when a console is actually reachable, following the health check that already runs
every few seconds rather than probing on its own, and turns red on hover so it reads as an off
switch rather than another grey icon.

### Fixed — a dead band across the top of every page
`logo.png` was **508×360 carrying only 489×177 of artwork** — 81px of transparent space above it and
102px below. Over half the image was nothing, and that nothing was being rendered as a full-width
empty band above and below the logo on every device.

The file is cropped to its artwork and the CSS heights are set to the exact size that artwork was
already drawing at (132 × 177/360 = 64.9px → **65px**, and 176 → 86px, 215 → 105px for the larger
breakpoints). **The logo renders at an identical size — 186.3px wide before and after.** Nothing was
shrunk, resized, or re-styled; only the emptiness is gone. The original is kept as
`assets/logo.original.png`.

### Changed — the library summary moved up beside the byline
*🎮 26 titles · 📂 C:\Mutant Games · 🔀 2 sources* had a row of its own above the grid. It now sits on
the same line as **By XavyProd v3.5.0**, right-aligned so its edge lands exactly on the settings
icon's edge. Same text, same size, one row fewer.

Measured at 1920×1020: header **222px → 141px**, and the first row of games moved from y=363 to
**y=250 — 113px of the page reclaimed** with nothing removed. Verified at 1920, 1366 and 375 wide:
no overlap, no horizontal overflow, and on a phone the summary drops below the search field instead
of wedging itself between the icons and the input.

## [3.4.0] — 2026-07-29 · "PCs find each other on their own, and the storage bar tells the truth"

### Fixed — PCs could not see each other unless the PS5 was up
Two companions on the same network, both running, both reachable — and neither found the other.
Everything went through the console, so with the PS5 off there was no cross-PC library at all, and
**moving a game between PCs depended on a console that has nothing to do with it**.

The cause was a timeout, not a network problem. Discovery probes the port, then asks the responder
to identify itself via `/api/federation` — with **2.5s** allowed. That reply is not a ping: the peer
builds a library listing to produce it. Casita answered in **3.006s**. Every sweep saw the open port,
timed out identifying it, and threw the peer away. The console never had this problem because the
ELF allows 6s, which is exactly why it only ever worked *through* the PS5.

Fixed at both ends:

- **We answer instantly now.** `/api/federation` was spending its entire 3s probing Payload Manager
  on the console — so with the PS5 asleep, *we* were the peer nobody could identify in time. The
  liveness value is cached, and the identity reply never probes at all. **3.02s → 0.010s.**
- **Discovery waits long enough** (8s) for a peer that is still on an older build, with one deadline
  for the whole sweep instead of four seconds per thread.
- **Peer-of-peer adoption.** Every companion advertises `known_pcs`; each PC now adopts them, so one
  machine finding another is enough for the whole set to converge.
- **A background keepalive.** Discovery only ever ran as a side effect of a request, so two idle PCs
  never met. A full sweep now runs at startup and every 5 minutes, with a cheap re-check of known
  machines every 20s in between.

Verified with the PS5 offline: **Casita found, online, 3 titles**; merged library **7 titles** with
correct host attribution; and a PC→PC move enqueued from Casita to this PC's folder with no console
involved at all.

### Fixed — the main page listed every storage slot, all of them empty
Whenever the console was unreachable the bar emitted **one tile per configured drive** — Internal,
EXT0, EXT1, USB0 through USB7 — each reading "0 games". Eleven tiles for storage that either was not
attached or held nothing.

A place now earns its tile by holding something. No console: no console tiles. No extended drive
attached: no Extended Storage tile (the console used to emit one from a failed `statvfs`). Nothing
anywhere yet: one honest line saying so, instead of a row of placeholders.

### Added — the PCs are on the storage bar
They are where the games live and what the console installs from, so they belong beside the drives:

    💻 ASUS-LAP (this PC)   14 GB   4 games
    💻 Casita               51 GB   3 games · 10.0.0.72

Real title counts and real sizes, on both the PC app and the console. A peer on an older build sends
no byte total, so its size is added up from the files it advertises rather than showing an empty tile.

### Fixed — storage was read once and never again
`loadStorage()` ran a single time at boot, *before* the API was even resolved. A drive that filled up
later, or a PC that came online, could not appear without a page reload. It now re-reads once the API
is settled and stays current, which is what makes a machine show up on its own.

## [3.3.0] — 2026-07-29 · "Stop all homebrew before rest mode"

### The rest-mode test result that changes everything
Shutting **only this app** down and then resting the console **still panicked**. That rules our
process out as the cause — which is the single most useful thing learned so far. The remaining
difference between a healthy console and this one is the *other* loaded payloads: **Elf Arsenal**
(plus the `dpiv2`, `nanoDNS` and `ftpsrv` processes it spawns, which outlive it), **GarlicSaves**,
and **ShadowMount**. None of them is part of the console's sleep/wake cycle either.

### Changed — the button now stops all of it, not just us
*Settings → Network → **Stop all homebrew before rest mode***.

It asks Payload Manager for the process list, stops each payload by name, and shuts this app down
last. Payload Manager itself keeps running, so everything can be loaded again **without
re-jailbreaking**.

Two deliberate design choices:

- **An allow-list, never "kill everything reported."** That list may well include system processes,
  and getting it wrong would take the console down harder than the crash being chased. Only names
  matching Arsenal / garlic / dpiv2 / nanodns / ftpsrv / shadowmount are stopped, and `pldmgr` and
  `elfldr` are protected outright.
- **Whatever is left is reported back.** The UI lists what is still running after the sweep, so the
  real process names can be read off a live console and the list widened precisely — rather than
  spending another 50-minute recovery on a guess.

### Added — it works from the PC and from a phone
The companion relays `/api/rest/prepare` to the console, the same way it relays the Helpers toggle.
You are about to walk away and rest the console, so pressing this from the PC beats having to open
the app on the PS5 first. Both console controls hide themselves when no PS5 is reachable.

`GET /api/rest/prepare?self=0` stops the helpers but leaves this app running — useful for checking
the sweep from a PC without losing the app in the same breath.

### Also fixed — the PC app crawled whenever the PS5 was off
Directly relevant, because this button's whole job is to leave you with a console that is *not*
answering. With the PS5 off or resting, the companion was making calls that could only ever time
out, one after another:

| with the console off | before | after |
|---|---|---|
| `/api/health` | 13.8s | **1.8s** |
| `/api/library` | 25.0s | **0.005s** |

Three causes, each fixed by asking first whether the console is even there:

- `running_title()` cost **two 6-second timeouts** on every health check. It is now only consulted
  when the console actually answered.
- `console_usb_packages()` allowed **20 seconds** for a console busy scanning sticks — right when it
  is there, entirely wrong when it is not. Skipped unless the console is reachable.
- `console_apps()` paid the full FTP timeout again on every call in the same request. A short
  10-second memo of the failure stops the retry storm; a success clears it immediately.

None of this changes behaviour when the console is up: every guard is the reachability signal the
response already reports. The library still lists the PC's own games, correctly marked
`console_reachable: false`.

### If it still panics
Then no app is responsible, and the remaining suspect is the jailbreak state itself — kstuff /
Y2JB's kernel patches, which no app-level change can undo. That would mean rest mode is simply not
safe on this setup until the jailbreak is reloaded, and the honest workaround is to power the
console off rather than rest it. The report from this button is what tells the two apart.

## [3.2.2] — 2026-07-29 · "The Helpers toggle works from the PC too"

### Fixed — "Could not change it" when pressing Helpers in the PC app
Helper auto-start is a **console** setting, and `/api/payloads/autostart` only ever existed on the
ELF. Pressing the button in the PC companion hit a **404**, so it failed every time — while the exact
same button worked on the PS5. Since the point of the toggle is A/B testing the rest-mode crash, it
has to work from wherever you happen to be sitting.

The companion now relays the call to the console, the same way it already relays cheat rescans and
mods. The button behaves identically on the PC, on the PS5, and on a phone.

Two smaller things went with it:

- **The button hides itself when no console is reachable.** There is nothing to toggle without a PS5,
  and a control that can only fail is worse than no control.
- **Failures say why.** It reported a flat *"Could not change it"* for every cause; it now passes
  through what actually went wrong (`no console configured`, `console did not answer: …`).

## [3.2.1] — 2026-07-29 · "The UI could not parse, so nothing loaded anywhere"

### Fixed — the app was blank on every device
A `confirm()` message added in 3.2.0 ended up with **real newlines inside a double-quoted
string** instead of `
` escapes. That is a syntax error, so the browser discarded the entire
script block — and the whole UI is one block. Both artifacts embed the same `web/index.html`,
so one bad token took out the PS5 app and the PC app at the same time.

What made it hard to see: **nothing looked wrong from outside**. Both servers stayed healthy and
returned `200` with the full page; the API answered normally. Only the page's script was dead.

### Added — the build now refuses to ship a UI that will not parse
`tools/check_web.py` runs before either artifact is packaged, from **both** `build-wsl.sh` (ELF)
and `PKG-MUTANT-SHOP.spec` (EXE), so it cannot be skipped by building one and not the other.
It parses the page with `node --check` when node is present, and always runs a scanner that flags
a string literal left open at end of line — the exact damage a generated edit causes. Quotes inside
comments, template literals and regex literals do not trip it. A failure aborts the build.

Verified after the fix: 25 cards render, filters read *All · Installed · Not installed · Updates ·
Add-ons (1)*, no console errors, and both devices serve a byte-identical page. The Mutant Cheat
Engine is untouched — 1954 json / 1760 shn / 1421 mc4 / 377 patches indexed, Bloodborne matching at
exact version — and download, install and queue endpoints all answer.

## [3.2.0] — 2026-07-29 · "Patches and DLC that live on a PC, and rest-mode safety"

### Fixed — a game's patch or DLC sitting on a PC never appeared on the PS5
The console merged each PC's library by title id and **skipped any title it already had**.
That is exactly backwards for add-ons: the console *does* have Bloodborne, which is precisely why
the v1.09 patch on the other PC mattered — and it was thrown away with the rest of the entry.
The same skip hid Castle Crashers' v1.04.

Peer titles the console does not have were also collapsed to a **single file**: the merge read
`items[0]` and gave the whole entry one kind, so a title a PC holds as base + update + DLC arrived
as one card with one file.

Both are fixed in the console's merge:

- every entry in the peer's `items[]` is read and bucketed into **base / updates / dlc**
- a title the console already has now gets the peer's add-ons **spliced into its existing card**
  instead of being dropped
- an add-on with no base stays an add-on (`update_only`), so it lands under **Add-ons** rather than
  posing as a full game
- the same patch held by two PCs is listed once

Verified live: Bloodborne now shows `update 01.09` from **Casita**, Castle Crashers Remastered shows
`update 01.04` from **ASUS-LAP**, and *Bloodborne The Old Hunters* is an Add-on, not a game card.

### Fixed — the console only ever found PCs that already knew its address
A PC announces itself every 8s, but a PC with no console configured never announces, so it stayed
invisible even when another PC could see it. Each companion now advertises `known_pcs`, and the
console registers those too — **one PC finding the console is enough for all of them**. No subnet
sweep on the console and no extra requests: the list rides along in the federation document already
being fetched.

### Fixed — Devices showed "0 titles" for every machine
`/api/network` on the console sent an empty `counts` block. It now reports the count from each PC's
own federation document and its own card count. Peers are named in the source list too, with titles.

---

### Rest mode — what changed, and what is still unproven

**The honest summary: the cause is not confirmed yet, and this release does not claim to fix it.**
What it does is remove our app's continuous background network activity and add a clean way out.

**What was found.** The app had *no* suspend or resume handling of any kind — nothing in it knows the
console is going to sleep, and a loaded payload is not part of the system's sleep/wake cycle, so
nothing quiesces it. On top of that it was doing steady network work: **every** `/api/library` and
`/api/storage` request opened a fresh TCP connection to **each** registered PC with a 6-second
timeout, and the PCs announce themselves every 8 seconds. Threads sitting in socket calls while the
network stack is torn down is the classic shape of a suspend-time fault.

**What was changed.**

- **Peer library snapshot** — the merged peer library is cached for 20s. A library request no longer
  opens sockets to every PC; the same answer is reused. Far less to be caught mid-flight at suspend.
- **Resume detection** — a watchdog compares `CLOCK_REALTIME` against `CLOCK_MONOTONIC`. Wall clock
  running ahead of monotonic means the console slept through it, so everything derived from the old
  network state is dropped and a notification confirms the app is awake.
- **Deactivate before rest mode** — Settings → Network now has a one-tap clean shutdown for the
  console. No API tells a payload that rest is coming, so a deliberate control is the truthful answer.
- **Helpers: auto-start / off** — the app launches **ShadowMount** and **Elf Arsenal** (which itself
  spawns garlic, nanoDNS on :53, and ftpsrv). Those are third-party payloads doing their own
  continuous network work and are the leading suspects. The toggle turns that auto-start off so the
  two cases can be told apart.

**How to find the actual cause** (each test is one rest cycle):

1. Settings → **Helpers: off**, reload the app, rest the console.
   *Survives* → the helpers are the cause, not us. *Crashes* → keep going.
2. Load the app, **do not open the UI**, rest the console.
   *Survives* → it is request traffic. *Crashes* → it is the payload merely being resident.
3. **Deactivate before rest mode**, then rest.
   *Survives* → confirms it is a live payload at suspend, and the shutdown habit is the workaround.

## [3.1.1] — 2026-07-28 · "Progress for games coming from another PC"

### Fixed — installing from a peer PC showed no progress
A game pulled from another PC sat at 0%, then jumped to **"submitted est."** while installing
perfectly in the background. Not a display glitch — the task genuinely had **no progress source**:

- `local_progress` was false (the source is the peer's name, not `companion-lan`), and
- `progress_url` stayed `None`, because it was only set for a *configured mirror*
  (`is_companion(src)`), and a **discovered peer** is not one.

With neither, `measurable` was false, so the worker submitted the install and reported
*"Sent to PS5 (progress not measurable for this source)"*. A game from the local library was
byte-counted normally, which is why one title behaved and the other did not.

The peer is the machine actually serving the bytes, so it knows exactly how many have gone out —
that is what `/api/served/<key>` reports. The peer lane now carries a `base_url`, and the
progress wiring accepts a peer as well as a configured mirror:

    peer_url   http://10.0.0.76:8710/library/<key>
    served_url http://10.0.0.76:8710/api/served/<key>

### Fixed — the queue hid the reason
`submitted` rendered as the bare state name, so a task that was explaining itself just looked
stuck. Queue rows now show the message, which is where the explanation lived all along.

Nothing in the install path itself changed: the same lane, the same daemon, the same files.
Only how progress is measured and reported.

---

## [3.1.0] — 2026-07-28 · "Mutant Multi Companion Engine"

Every device now shows the **same** library, with the same categories and the same artwork,
regardless of which machine you happen to be looking at.

    before          ASUS-LAP 25 titles, no Add-ons | Casita 26, Add-ons(1) | PS5 26, no Add-ons
    after           all three: 26 titles, Add-ons(1), identical contents

### Fixed — cover art never crossed the network
Artwork lives on whichever PC holds the game, and nothing advertised it, so every remote title
fell back to drawing initials (the "CC" / "ZA" / "BT" tiles). Peers now advertise `has_icon` and
an `icon_url` pointing at their own `/icon/<tid>.png`, the merge carries it, and a device with
no artwork of its own **borrows the peer's**. This was not the virtualization — that only
decides which cards exist, never what they contain.

### Fixed — the console turned every peer title into a fake "game"
The console's merge emitted everything as a `base` entry, so a DLC or update from a PC became a
game card and the **Add-ons category could never appear on the PS5 at all**. It now carries the
item's kind into the right bucket and preserves `update_only`, so an add-on stays an add-on
everywhere.

### Fixed — Settings said no PCs were found, while merging their libraries
`/api/network` only existed on the companion. The console has it now, so the Devices panel
lists the machines it is actually talking to — and it learns their **names** from what they
advertise instead of showing bare addresses.

### Fixed — "1 source · auto-picked" however many PCs were connected
The source list only counted download mirrors, never the other machines supplying games. Peers
are sources now, on both the companion and the console:

    console sources: this PS5 · ASUS-LAP · Casita

### Note
A PC only advertises artwork once it is running **3.1.0** — update every machine, or titles
coming from an older one still show initials.

---

## [3.0.0] — 2026-07-28 · "One library, every device"

### Fixed — the console only ever knew about ONE PC
`g_pc_url` was a **single slot**. Every companion announces itself to the console, so the
second PC to start simply overwrote the first — which is exactly why the library changed
depending on which computer you had open.

The console now keeps **every** PC that announces itself (`/api/pcs` lists them), asks each one
what it holds, and merges the lot into its library. A PC that is asleep or off is skipped, so
nothing depends on any particular machine being awake.

    PCs known to the console: 10.0.0.76 (ASUS-LAP) + 10.0.0.72 (Casita)
    PS5 library: console 23 · ASUS-LAP 1 · Casita 1   (the rest dedupe — already installed)

### Fixed — add-ons were invisible from other devices
`federation_self` only advertised titles that had a **base** game, so an update or DLC sitting
alone on one PC was never sent to the others — which is why the Add-ons category appeared on
Casita and nowhere else. Every installable file is now advertised with its kind, and the merge
puts each one in the right place: a peer's DLC lands in that game's **DLC** section, and an
add-on with no base stays an add-on. The category is therefore the same on every device.

### Changed — filter order
**All · Installed · Not installed · Updates · Add-ons**

### Added — virtualized grid
Only the cards near the viewport exist as DOM nodes; spacers above and below keep the scrollbar
honest. Below 60 titles it stays off, because virtualization has its own cost and a small
library is faster without it. Measured with 400 titles: **90 nodes instead of 400**, window
tracking correctly while scrolling (`[0,47] → [42,131]`, `padding-top: 2447px`).
- Row height and column count are measured once and the window computed arithmetically — no
  per-card measurement, which would defeat the purpose.
- It binds to the **real scrolling container** (`<main>`), not the window: this page does not
  scroll the document, so a window listener would never fire.
- Detection tests the overflow **style**, not whether the element happens to overflow at that
  instant — during a rebuild the container is briefly empty and would resolve to the wrong node.
- The scroll handler schedules with `requestAnimationFrame` **and** a timer guard: some embedded
  browsers throttle rAF to nothing when not compositing, which would silently stop the grid
  updating mid-scroll.

---

## [2.9.0] — 2026-07-28 · "Smooth on the console, and PC to PC"

### Fixed — the PS5 showed FTP off and everything disconnected
A real bug, and console-only. `refreshHealth()` handled the on-console case with an **early
return placed before any status was painted**, so every LED kept its initial "off" and every
row kept its "-" — while the services were running fine. The PC never hit it.

The console also reported two **hardcoded** booleans (`ftp_online:true`, `dpi_online:false`)
instead of looking. It now probes FTP (2121), the install engine (12800) and ShadowMount (9021)
on loopback and reports what is actually there, plus its real LAN address.

`/api/sources` on the console had none of the fields the header bar reads, which is why it said
"(none configured) · 0 sources". It now reports the console itself as the source, with the
folders it really scans.

### Changed — helper startup is framed instead of mysterious
The burst of `Debug:` toasts on the first run after a boot comes from the helper payloads
themselves (garlic, nanoDNS, ftpsrv, the install host) as they start. **Nothing in our process
can suppress another program's notifications** — so instead:
- We now say what is about to happen *before* launching them, and post **one** summary
  afterwards: `Mounts on · Installs on · FTP on`.
- The duplicate generic "ready" toast is gone — the summary replaces it.
- `GET /api/payloads/autostart?on=0` stops the shop launching helpers at all, so no third-party
  startup messages appear. Start them yourself from Payload Manager when you want them.
It only happens once per boot because on later runs the ports are already busy and nothing is
launched — that behaviour is unchanged.

### Added — copy a game from one PC to another
A peer already serves its library over HTTP with byte ranges (that is how the console installs
from it), so pulling a copy onto this machine is the same fetch written into the library folder.
**⋯ → Copy from &lt;PC&gt;** on any title that lives on another PC.
- Downloads to a `.part` file and renames only when complete, so an interrupted copy can never
  be mistaken for a finished game.
- Resumes with a Range request if a partial is already there, and verifies the final size.
- Runs as a normal queue job, so it shows progress and can be cancelled.

### Performance — the parts that actually apply to a console browser
- **The grid no longer rebuilds unless it changed.** It was torn down and re-created on every
  health tick. A cheap signature guards it, and a no-op render now leaves the DOM completely
  untouched — verified in-browser.
- **Cards are built in a DocumentFragment**, so 25 cards cost one layout and one paint instead
  of 25 of each.
- **Search is debounced** (140ms) — it used to filter and rebuild on every keystroke.
- **Stale-while-revalidate:** the last library is kept in localStorage and painted instantly on
  open, then replaced when the real one arrives, instead of staring at an empty grid.

Not applicable and deliberately not done: pgvector, SQL/vector search, quantization, connection
pooling, edge/object caching. This is a stdlib-only companion plus a C payload serving one HTML
file to one console over LAN — there is no database, no CDN and no embeddings, and adding them
would break the self-contained single-EXE design.

---

## [2.8.0] — 2026-07-27 · "Add-ons know their place"

### Fixed — the version flashed 1.1.1 on every load
The UI carried a hardcoded `APP_VERSION` used until `/api/health` answered, and it had been
left at an old number. Both artifacts bundle `web/` directly, so `tools/stamp_version.py` now
writes the real version into the page at build time from one source (`companion/server.py`),
and `build-wsl.sh` runs it. It also warns if the EXE and ELF versions ever disagree.

### Fixed — DLC and patches appeared as their own games
An update or DLC whose base game is nowhere — not in the library, not on the console — was
getting a full game card next to the real title, showing the ADD-ON's ids as if they were the
game's. Those are now hidden from the grid. An **Add-ons (n)** chip appears when any exist so
nothing is silently swallowed, and opening one says plainly which title it belongs to, with its
own id and content id visible — so a file that is actually for another region or release is
obvious instead of quietly wrong. When the base game arrives they attach automatically, because
grouping was always by title id.

### Added — multi-part packages install as one thing
Some releases ship split across `Part 1` / `Part 2` (or `pt2`, `[1 of 3]`, `.part2`, `Disc 2`).
Installing one piece leaves the title broken. Parts are now recognised as a **single library
entry** carrying an ordered `parts` list, and installing it queues every piece **in order** —
the queue already serialises per console, so they install one after another. Works the same
from a PC library, a peer PC or a USB stick, because expansion happens before the lane is
chosen.
- A release with a **gap** in its numbering is refused with an explanation rather than
  installing a knowingly incomplete set.
- Unit-checked against real names: `v1.04` and `[6.72]` in existing filenames are **not**
  mistaken for part numbers.

### Fixed — a freshly applied patch took several refreshes to show
`console_apps()` caches installed titles for 30s, so the panel kept reporting a patch as
pending until the cache aged out. Any job reaching "playable" now invalidates that cache and
bumps the library generation, so the UI updates as soon as the install lands.

### Added — cheats say which ones actually work on your build
Compatibility was per-FILE; it is now per-MOD. When the game is running, a mod whose bytes match
neither its ON nor its OFF state targets code this build does not have — it can never apply. Those
are greyed out and labelled **N/A** with the reason, instead of letting you press them and get a
refusal. Where the file does not match the installed build, the note now lists which versions
your library *does* have cheats for (`/api/cheat/list` and `/api/mods` both report `versions`).

---

## [2.7.0] — 2026-07-27 · "More than one PC, and your phone"

### Added — every PC on the network is one library
A second computer only has to **run the app**. It is found on the LAN automatically, and its
games join this library — no pairing, no URLs to type.

- **Stable identity.** Each install writes a device id into its config once and keeps it. A
  hostname is neither unique nor stable, so peers key on the id: rename the PC or let DHCP
  change its address and it is still recognised as the same machine, not a duplicate entry.
- **Discovery** sweeps the /24 for the companion port, then asks each responder to identify
  itself, so a library is only ever merged from something that really is one of ours. It runs
  in the background — a library request never waits on a scan.
- **One card per game, however many PCs have it.** Titles are merged by title id and carry
  `hosts` — who has a copy. Verified with two libraries sharing Castle Crashers: one card,
  `hosts=['ASUS-LAP','DESKTOP-PS5RIG']`, no duplicate.
- **Peer-only games install directly from the PC that has them.** The console fetches from that
  machine, so nothing is proxied through this one and the transfer does not depend on this PC
  staying awake. This is the piece that was missing: `peer_url` was being produced and never
  consumed, so installing a peer title returned *"unknown install_key"*.
- Panels show where a game lives (**Located on**, **Also on**, **Source PC**), and a card for a
  title that only exists elsewhere is badged with that PC's name.

So one machine can hold the PS4 library and another the PS5 library — or both hold both — and
the console sees a single merged catalogue either way.

### Added — control it from a phone
The companion already listened on all interfaces; what was missing was the address and the
ergonomics. **Settings → Devices on this network** shows the URL to open on a phone, this PC,
and every peer found. On a phone the mod toggles are now **62×43** instead of 50×35 (44px is
the minimum a finger reliably hits), and hover tooltips are suppressed on touch, where they
can never be shown and only get in the way. No desktop layout changed.

### Fixed
- `/api/devices` was already taken — it returns **storage drives**, and the drive selector
  depends on it. The new machine list is `/api/network`; adding it under the old name would
  have silently shadowed the drive list and broken install destinations.
- Settings scrolled again once the Devices block was added (columns 420 vs 659). Rebalanced to
  **552 / 425** and tightened the row spacing: **no scrolling**, verified at 720p.

### Not done yet
Moving a game *between two PCs* is not implemented — right now a peer's games are installed to
the console, not copied PC-to-PC. Say the word and that is the next piece.

---

## [2.6.1] — 2026-07-27 · "+ Queue actually queues"

### Fixed — queueing a USB package installed it immediately
The console-local lane added in 2.6.0 called the installer straight from the request handler
and ignored `mode: "queued"` entirely, so **+ Queue** behaved exactly like **Install**.

It is a proper queue lane now, the same as every other install: `+ Queue` adds it **held**
("Held — press Start"), and it only runs when the queue is started. The job appears in the
queue with live progress, and cancel works while it is held.

Reused the existing `submitting` / `promoting` states rather than inventing an `installing`
one — a new name would have fallen outside the `RUNNING` set, quietly breaking both
"Clear done" and the parallel-install limit.

### Fixed — the console behaved differently from the PC
With the companion off, the on-console app had no queue at all (`/api/queue` always returned
an empty list), so the same button meant two different things depending on whether the PC was
running. The console now holds, starts, cancels and clears the same way, over both GET and
POST — the UI drives the queue with POST, which the console previously answered with
"not supported on console".

---

## [2.6.0] — 2026-07-27 · "Installing from a stick, properly"

### Fixed — a USB install produced an unplayable game
The USB path called `sceAppInstUtilAppInstallPkg`, which **only registers metadata**. It
returns in seconds, creates a dashboard tile, and leaves no game data — the tile then fails
with *"Cannot start the game"*. The download pipeline has warned about exactly this in a code
comment for months; the USB path simply used the wrong call.

A package already on the console now goes through the **same pipeline as a downloaded one**:
it is served over HTTP and handed to the install daemon, which fetches and installs it for
real. Verified on-device — `/mnt/ext1/user/app/CUSA03474/app.pkg` is **582,549,504 bytes**,
the complete package, with the same file layout (`app.pkg`, `app.pbm`, `app.json`, `app.xml`)
as a known-good install.

Three things had to be right for that to work:

- **A Range-capable file server.** The daemon HEADs for the size, then fetches with byte
  ranges. `/pkgfile/…` answers `HEAD`, `200` and `206 Partial Content` with `Content-Range`.
  Paths are restricted to package/backup files on drives we scan; traversal and anything else
  is refused (verified: `/etc/passwd`, `../../etc/shadow` and a `.html` all rejected).
- **Clean URLs.** The daemon **cannot fetch a percent-encoded URL**. A real name like
  `Star Wars Racer Revenge - LUA C0RE EXPLOIT - UP1082-CUSA03474.pkg` came back
  `{"res":"-1","error":"install failed"}`, while the identical request with a clean name
  returned `{"res":"0"}`. Packages are now served under a short opaque token
  (`/pkgfile/<n>.pkg`) — the same trick the PC path already used with `/library/<key>`.
- **Off the main thread.** The daemon installs **synchronously**: the POST does not return
  until it finishes. Running that on the accept loop froze the whole app — UI, cheat engine
  and even `/api/installed` stopped answering mid-install. Both the transfer and the install
  now run on their own threads, with `GET /api/install/status` for progress.

With the companion running, `local:` install keys are forwarded to the console instead of
being looked up in the PC file registry.

### Fixed — no notifications appeared on the PS5
Every toast had been silent since the app icon was added. The shell **accepts** the icon form
(`rc = 0`) but does not render it on this firmware — so the app reported success while nothing
appeared, which is the worst possible failure mode. All notifications are back on the plain
form, byte-for-byte what was confirmed working on-device.

The icon variant is kept behind `GET /api/notify?icon=1` so it can be compared on the TV
without putting the working path at risk again.

---

## [2.5.1] — 2026-07-27 · "The toggle that killed the app"

### Fixed — applying a cheat crashed the whole on-console app
Toggling a mod took the server down: the browser reported *"remote end closed the
connection"*, every later request failed, and no notification could appear because the
process was gone. The cause was a printf argument list, not the cheat engine:

```c
notifyf("%s is %s\n%s%d memory patch%s applied",   /* declares 5: %s %s %s %d %s */
        what, "ON", title, " - ", wr, "es");       /* passes 6                    */
```

`%d` consumed the `" - "` **pointer**, and the following `%s` then received the integer
`wr` and dereferenced it — a segfault inside `vsnprintf`. It only fired when a write
actually landed (`rc > 0`), which is why the same line survived every earlier test: those
runs returned `skipped=1` and took the "already in that state" branch instead.

The memory engine was never at fault. `mem_read`, `mem_write`, the page-walk, expect-gating
and address resolution were all working the entire time.

Both occurrences fixed, and **`notifyf` is now declared `__attribute__((format(printf,1,2)))`**
so the compiler rejects this class of bug instead of letting it reach the console. Every
formatted call in the file was audited: **191 checked, 0 mismatches.**

### Fixed — installing from a USB stick said "unknown install key"
With the companion running, the UI posts installs to the PC, which looked the key up in its
own file registry. A `local:` key points at a file already on the console, so it was never
going to be there. Those now route to the console app, which installs from the stick with
the same call it uses standalone (`lane: "console-local"`). Keys that genuinely are unknown
still return `unknown install_key` exactly as before.

### Verified after the fix
- Doom Eternal running (pid 724): 4 mods listed with live state, `compatible: true`.
- Toggle through the companion — the exact path that crashed — returns `written=1`, the app
  stays up, and re-reading the game's memory confirms the new state.
- Notifications deliver with `rc=0`, icon included.

---

## [2.5.0] — 2026-07-27 · "Removable media, told honestly"

### Fixed — a package on a USB stick claimed to be installed
`/api/installed` was built by scraping every `"title_id"` out of the library JSON, so a package
merely *sitting* on a stick came back as installed: green INSTALLED badge, "✓ Installed" in the
panel, "Reinstall base" as the action. It now comes straight from `app.db`, which only knows
about titles the console has actually registered.

### Fixed — the USB game was filed under "PC"
`isOnPC()` meant "has an installable copy", which a USB package also has. It now means what it
says — a copy in the **PC library folder** — and removable media is its own thing. Added a
**USB filter chip**, so the four categories are All / PS4 / PS5 / PC / USB.

### Added — PS4 and PS5 packages are no longer treated the same
They genuinely differ, and the app now says so instead of guessing:

| On removable media | What it is | Can it run there? |
|---|---|---|
| `.pkg` | a package to install | **No** — a PS4 title must be installed first |
| `.ffpfsc` / `.ffpfs` / `.ffpkg` | a ShadowMount backup | **Yes** — ShadowMount scans USB roots and `<drive>/homebrew` |

Each entry now carries the drive it is on (`USB 0`, `Extended Storage`, …), its real path, and
whether it runs in place. The card badge shows the **drive** rather than a bogus install state,
and the panel gained **Located on**, **Runs from here** and **Path** rows.

### Added — move a game between drives
A PS5 backup runs from wherever it sits, so "installing" one really means putting it on the
drive you want. **⋯ → Move to another drive…** lists every mounted drive with its free space and
copies on the console — no PC involved. It **copies, verifies the size, and only then deletes
the original**; a half-copied 90 GB game that had already removed its source would be
unrecoverable. Destinations are the paths ShadowMount actually scans (read from its own
`config.ini`), so a moved game is picked up with no further setup. Progress and completion are
reported by toast; `GET /api/move/status` has the live percentage.

### Fixed — duplicated games and phantom drives
Scanning drives for backups listed every `ext1` game a **second** time, next to the copy already
read from app.db (41 entries instead of 23). Removable entries are now skipped when the title is
already registered. Separately, an unmounted `/mnt/usbN` still stats fine because it resolves to
the parent filesystem, which listed **six drives that do not exist** with 0 GB free; a drive now
has to be its own filesystem (`st_dev`) to count. Result: **23 games, no duplicates, 3 real drives.**

### Changed — settings fit on one screen
The panel used a two-column auto-flow, which dropped both tall blocks into the same column and
forced scrolling. It now uses explicit columns, grouped as **Console · Services · Games library**
and **Cheat & mod library · Download sources · About**, with denser rows and long paths
shortened rather than wrapped. `[VERIFIED]` in a browser: content height equals the visible
height — **no scrolling** — with the two columns at 420 px and 442 px.
The "I own the content I install" checkbox is removed.

---

## [2.4.0] — 2026-07-27 · "The console app stands on its own"

The on-console app depended on the PC for things it could do itself. With the companion
switched off it showed title IDs instead of names, no versions, no cheats and dead settings
rows. All of it is fixed at the source. `[VERIFIED]` end to end with the PC companion closed.

### Fixed — the console could not read its own library
It built the library from **file names** (`/mnt/ext1/homebrew/*.ffpfsc`, `/user/appmeta/CUSA*`),
which is why panels showed `CUSA08692` as the game name with no region, size or version.

Real data lives in `app.db`, which is SQLite — the companion reads it with Python, the ELF had
no way to. It does now: a **read-only single-table SQLite scanner** (~240 lines: b-tree walk,
record decoding, overflow-page chains, column lookup parsed from the CREATE TABLE statement so
a firmware change cannot silently shift reads onto the wrong field). No writes, no journal, no
locking, no SQL. **22 titles now read with correct names, versions, regions, sizes and install
locations with the PC off** — identical to what the companion reports.

### Fixed — cheats were unreachable on the console
The UI is one codebase served from both the PC and the console, but the console answered only
its own `/api/cheat/*` names. The panel calls `/api/mods/<TID>`, which fell through to the
catch-all `{}` and rendered **"Can't reach the PS5 mod engine"** — while the engine was running
right there. The console now answers the same endpoint names as the companion:
`/api/mods/<TID>`, `/api/cheats/paths`, `/api/cheats/rescan`, `/api/sources`, `/api/consoles`,
`/api/queue`, `/api/config`.

**The server was also GET-only**, so every cheat toggle got a `405`. It now handles POST with a
correctly-drained body (`Content-Length` is authoritative — a short read here is the same bug
that once hung the PC side): `/api/mods/<TID>/toggle | apply | select | disable-all`.

### Fixed — settings showed dashes for services that were running
Those rows were filled in by the companion. The console now probes **its own ports** on
loopback (ShadowMount 9021, install host 12800, FTP 2121) and reports its real LAN address, so
`ftp://<ps5-ip>:2121` and the cheat folder paths are shown on the console itself.

### Fixed — loading the payload twice said nothing
`notify()` hands off to a detached thread; the "already running" path called it and then
returned from `main()`, killing that thread before it could deliver. Exit-path toasts are now
sent **synchronously** (`notify_sync`). The message says which it was and where to go.
Confirmed separately that the icon request is fine: the kernel returns **rc=0** for the
extended struct, and there is now a fallback to the smaller layout if it ever refuses.

### Added — install from USB with no PC at all
`/mnt/usb0..7` (root and `/pkg`) are scanned for packages, and `POST /api/install` installs one
straight from the stick through the installer already proven to work in this process. Packages
without a CUSA/PPSA id are skipped, so the PS3 packages on the same stick are not offered as
installable PS5 games. **The companion now shows the console's USB packages too** — matched by
title id so a stick copy never duplicates a title already listed.

### Removed
CheatRunner, completely: `/data/cheatrunner`, its Payload Manager copy and the Desktop ELF —
after verifying our own library was intact and that neither artifact depends on it (the ELF
never references port 9999; the companion falls back cleanly). Its process keeps running until
the next reboot, but it has no data left to serve. **The `CHTR09999` home tile is a system
registration and is not removed by deleting files** — delete it from the PS5 home screen if you
want it gone.

---

## [2.3.0] — 2026-07-27 · "Every game reads completely"

### Fixed — PS5 titles reported no version at all
PS4 and PS5 do not store the version in the same field, and we only ever read the PS4 one:

| Platform | Field | Example |
|---|---|---|
| PS4 (CUSA) | `APP_VER` | `01.04` |
| PS5 (PPSA) | **`CONTENT_VERSION`** | `01.011.000` |

Every `PPSA` title therefore came back blank, so the cheat engine could never confirm a match
and every PS5 panel showed no version. **Version coverage went 0/19 → 19/19 on PS5.**
Compatibility is now genuinely verified rather than assumed: 8 of 10 titles with cheats match
exactly, and the two that do not are real mismatches — DRAGON BALL: Sparking! ZERO (game
`02.000.017` vs cheats `01.000.000`) and Dark Souls Remastered (`01.00` vs `01.03`).

### Fixed — game panels were not equally complete
Whole sections silently vanished when a title had no copy in the PC library folder, which made
console-only games (nearly all the PS5 ones) look half-empty next to PS4 games. Every panel now
renders the same sections — **Details · Base game · Updates & Patches · DLC · On console ·
Mods & Patches** — with an honest explanation where a section is empty instead of no section.
`Base game` for a console-only title is deliberately **information only, with no install
control**, so nothing in that branch can reach the install path.

`Details → Version` now prefers what is actually installed over the version of the PKG sitting
in the library: a patched game reports `01.04` while its base PKG still says `01.00`, and the
installed one is the truth the cheat engine matches against.

### Added — notifications carry our icon and say something useful
The shell's notification request has icon fields we were not using. Toasts now show the app's
own artwork (`icon0.png`, already extracted to the console at boot) and describe what happened:
- `Inf Ammo is ON` / `DOOM Eternal · 2 memory patches applied`
- `Inf Ammo was already ON` / `Nothing needed changing`
- `Inf Ammo not applied` / `The game's code does not match this cheat file — it is probably
  built for another version. Nothing was written.`

### Added — cheat files are found automatically, wherever you put them
- Drop files in **`/data/pkg-mutant-shop/cheats/incoming`** (reachable at
  `ftp://<ps5-ip>:2121`) and they are filed automatically. A **`/cheats` folder on any
  plugged-in USB stick** is picked up too, and never deleted from the stick.
- Sorted by **what is inside the file, not its name** — a `.shn` saved as `.json`, or a `.mc4`
  saved as `.txt`, still lands correctly. `.mc4` is only accepted if it actually decrypts to
  Trainer XML. Unrecognised files are left alone rather than guessed at.
- `GET /api/cheat/paths` lists every watched location; **Settings → Cheat & mod library** shows
  them with a **Rescan cheats** button (`GET /api/cheat/rescan`) so new files are picked up
  without reloading the payload.

### Removed
- The old CheatRunner cheat and patch directories, after verifying our copies were complete and
  byte-identical (1954 json + 1760 shn + 1421 mc4 + 377 patches). CheatRunner's own app files
  were left alone — removing those is a separate decision.

### Performance
Measured rather than assumed: a cheat-file lookup takes **17 ms** against a **13 ms** bare
health-check baseline, so the per-request directory scan is already at network cost. No index
was added — it would have been complexity for nothing, and scanning fresh each time is what
makes a newly dropped file usable immediately.

---

## [2.2.0] — 2026-07-27 · "Every cheat format, and our own library"

Mutant Cheat Engine now reads **all three** cheat formats and keeps its library in our own
directory. `[VERIFIED]` on-device: 120 files parsed on the PS5 with zero failures.

### Added — .shn and .mc4 support
- **`.shn`** is a plain-XML "Trainer" document. **`.mc4`** is the same XML, base64-encoded and
  AES-256-CBC encrypted. Both convert to the JSON document the engine already parses, so the
  working JSON path is reused verbatim rather than duplicated.
- **AES-256-CBC decryption written from scratch** (decrypt only — we never write cheat files).
  The S-box is generated at runtime instead of carried as a literal table.
- Verified by converting `.shn` and `.mc4` for a title that also ships a `.json`: all three
  produce a **byte-identical** document.
- **This unlocked 5 more of the installed games** — Hogwarts Legacy (11 mods), Dying Light 2
  (15), Crash Bandicoot 4 (7), Crimson Desert, Dragon Ball: Sparking! ZERO — all of which
  previously reported no cheats because only `.json` was readable.

### Added — our own library directory
- The ELF creates **`/data/pkg-mutant-shop/cheats/{json,shn,mc4,patches}`** at boot and migrates
  the old CheatRunner tree into it on a background thread: **5135 cheat files + 377 patches**.
- The source directory is **left intact**. The copy costs nothing and means a failed migration
  can never lose someone's cheats; lookups fall back to it while the copy is still running.
- `GET /api/cheat/library` reports counts per format and migration status.

### Fixed
- **Toggling a mod no longer redraws the panel.** It rebuilt the whole drawer on every click,
  which read as a flash. Conflicts still change which mods can be toggled, so state is still
  re-read after every write — it now patches the existing rows in place. Verified with a
  MutationObserver: **0 rebuilds**, and the row survives as the same DOM node.
- **Limits were below what the real library contains.** Measured across a 400-file sample:
  74 mods in the largest file (cap was 64 — the rest were silently dropped), 41 entries in the
  largest mod, and a 520-byte patch (cap was 256). Now 256 / 128 / 1024. Entry arrays moved to
  the heap; at the new size they would have blown the thread stack.
- `hex2bytes` accepts the `90-90-90-90` separators these formats use. JSON files contain none,
  so that path is bit-for-bit unchanged.

### Safety
- `.shn`/`.mc4` offsets are ambiguous — most image-relative, a few absolute, with nothing in the
  file reliably saying which. We resolve it by **reading both candidates** and keeping whichever
  already holds a state the file documents. Relative is probed first because a raw absolute
  offset can land on GPU/MMIO and hang the game on a mere read. Entries whose ValueOff is one
  repeated byte (a zero-filled code cave — 38% of real entries) cannot discriminate, so the mode
  is decided from a mod's distinctive entries and applied to the rest.
- If nothing is conclusive we use relative and let expect-gating decide: a wrong guess **fails
  closed** rather than writing over unrecognised code. JSON addressing is untouched.

---

## [2.1.2] — 2026-07-27 · "The mods panel actually renders"

The real reason cheats never showed. `[VERIFIED]` end-to-end in a browser against running Doom.

### Fixed
- **The whole mods panel crashed whenever it tried to show a note.** `cheatNote()` returns HTML
  *text* — it is assigned to `innerHTML` everywhere else — but four call sites passed it to
  `body.appendChild()`, which throws `TypeError: parameter 1 is not of type 'Node'`. The panel's
  `.catch()` swallowed the error and printed **"Could not read mods."**, so a backend that was
  returning four mods correctly looked like a title with no cheats.
  This was latent from the start: before 2.1.1 the `!compatible` note tripped it, and after
  2.1.1 the new "version can't be confirmed" note tripped it in exactly the same way.
  Notes now go through `addNote()`, which uses `insertAdjacentHTML` — the string/Node mismatch
  cannot recur. Audited every other `appendChild` call site; `cheatNote` was the only offender.

### Verified
- Doom Eternal (PPSA01981, running): panel lists 4 mods with live state, toggles enabled.
- Toggling **Inf Jumps** from the UI and re-reading the console's memory returns `on`.

---

## [2.1.1] — 2026-07-26 · "Cheat search that doesn't give up"

Fixes a title with cheats reporting **"no cheats"**. Doom (PPSA01981) was the case that exposed
it, but every PS5 title was affected. `[VERIFIED]` on-device against a running game.

### Fixed
- **An unknown game version was treated as a version mismatch.** PS5 (`PPSA…`) titles often
  expose no `APP_VER`, so `installed_version` came back empty and every cheat file was flagged
  incompatible. Not knowing a version is not the same as knowing it's wrong — the mismatch
  warning now only fires when we can actually prove a mismatch. The per-write safety check
  (memory must already hold the expected bytes) is unchanged and still gates every write.
- **Mods could only be found for the game currently running.** For any other title the request
  fell through to CheatRunner, and if that wasn't loaded the panel dead-ended with
  "CheatRunner is not running". Our engine now resolves cheat files for **any** title.
- **The installed version was never sent to the file picker**, so `/api/cheat/running` could
  never hit its exact-match branch and always reported `other version`. It's passed now —
  Doom resolves as `exact version`.
- **Empty-state text still named CheatRunner**, a dependency we removed in 2.0.0.

### Added
- `GET /api/cheat/find?title=<TID>[&version=]` (on-console ELF) — the cheat file we'd use for a
  title, running or not. Same precedence as before: exact version → generic `<TID>.json` →
  any other version. Title ids are character-filtered before being echoed into JSON.
- Mods for a **non-running** game are now listed read-only, with toggles disabled and a tooltip
  explaining the game must be launched (writes go into its live memory).
- Compatibility banner has three states instead of two: **Matched** (verified),
  **Loaded** (console reports no version — can't confirm), **Version mismatch** (proven).

---

## [2.0.0] — 2026-07-26 · "Mutant Cheat Engine — our own"

Cheats, mods and patches now run on **our own engine**. Nothing in the running-game path
depends on a third-party service any more.

### Added — the engine (in the on-console ELF)
- **Memory I/O without ptrace** — the game is never stopped. Reads via `mdbg_copyout`. Writes
  can't use `mdbg_copyin` (broken on FW 8.20+), so we resolve the address ourselves:
  `proc → p_vmspace → pmap → {pm_pml4, pm_cr3}`, derive the direct-map base from their
  difference, walk PML4→PDP→PD→PT, then `kernel_copyin` to the physical page. This bypasses
  page protection, which is required — cheats patch read-only code pages.
- **Cheat parser** — hand-rolled scan of the real schema (`mods[].memory[]` with
  `offset`/`on`/`off`; offsets are hex without `0x`, applied at `eboot_base + offset`).
  No JSON library, so the payload stays dependency-free.
- **Apply / revert with a hard safety gate.** Before writing `on` it verifies memory currently
  holds `off` (and vice versa). Code that doesn't match what the cheat expects is **refused,
  not forced** — a cheat built for another build cannot corrupt unrelated memory. Entries
  already in the target state are skipped. `force=1` is opt-in. Every call reports
  `entries/written/skipped/failed`.
- **Our own running-game discovery** — `sceSystemServiceGetAppIdOfRunningBigApp()` for the app
  id, `sysctl KERN_PROC` (ki_pid at +72) matched against `sceKernelGetAppInfo` for the pid, and
  `kernel_dynlib_mapbase_addr(pid, 0)` for the image base instead of assuming `0x400000`.
  Verified to agree exactly with the reference implementation.
- Endpoints: `/api/cheat/running`, `/api/cheat/list`, `/api/cheat/apply`, `/api/mem/read`,
  `/api/mem/write` (the last gated by `expect=`).

### Fixed
- **Notifications never rendered.** `sceNotificationSend` with a JSON payload returns 0 and
  draws nothing. The shell only renders `sceKernelSendNotificationRequest` with a flat struct
  (45 reserved bytes + message), sent on a detached thread. Toasts now appear on the TV —
  mod on/off and patch-applied included.
- **Mods always showed "off".** The engine reports `state`/`label`
  (`"on"|"off"|"conflict"`) plus `canToggle`/`conflictsWith` — not `on`/`enabled`. Reading the
  wrong fields made every mod look disabled even when active. Conflicts are now surfaced, with
  the conflicting mod named in the tooltip.
- A cheat file must be **selected** before its state is meaningful; this is now automatic.
- The running game sorts to the front of the library with a **Running** badge, and returns to
  its normal position when it stops.

### Verified on-device (Castle Crashers CUSA14409, pid 437, base 0x400000)
`engine=mutant` · discovery matches · `apply mod0 ON → entries=2 written=2 skipped=0 failed=0`
· `revert → written=2` · TV toasts shown.

### Licence
The engine is derived from CheatRunner (GPL-3.0), which the project owner authorised. At
release PKG MUTANT SHOP must ship under **GPLv3 with source**.

### Not yet
`.shn` / `.mc4` cheat formats. Titles that are not running still browse through CheatRunner
(applying to a stopped game is meaningless), so it remains a fallback rather than a dependency.

---

## [1.3.0] — 2026-07-26 · "Installs work, unattended"

Confirmed on-device: games download from the PC and install to the PS5 as **playable titles**,
multiple in a row, with nothing loaded or reloaded by hand.

### Fixed — the mistake that caused ~12 rounds of false diagnosis
- **Installed games live in `/mnt/ext1/user/app/<TID>/` (Extended Storage), not `/user/app/`.**
  A missing `/user/app/<TID>` was being treated as proof that installs were fake. It wasn't — it
  was the wrong path for this console. Real installs verified: `app.pkg` at 4.05 GB / 7.27 GB plus
  `app.pbm`, `app.pbm.backup`, `app.json`, `app.xml`. Always check BOTH locations
  (`contentLocation=2` in app.db means the title is on ext storage).
- **`sceAppInstUtilAppInstallPkg` registers metadata only** — it creates `/user/appmeta/<TID>` and
  no game data, which is what produced dashboard tiles that failed with "Cannot start the game".
  It is no longer used as an install path.

### Added
- **The app starts the install host itself** (`Ps5Bridge.ensure_dpi_host()`): if the install
  daemon isn't listening when a game is queued, the app launches it via Payload Manager and waits
  until the install lane actually answers. No manual payload loading.
- Combined with the existing wedge recovery, a queue of several games now runs start-to-finish
  unattended: host started on demand, daemon healed between installs, smallest-first ordering.
- Reclaimed ~15 GB on the console: the old local download cache under
  `/data/pkg-mutant-shop/install/` is not used by this path (the host fetches the PKG itself).

### Verified end-to-end
Install host NOT running → queued Marvel + Dark Souls → app started the host → both installed
unattended → both **playable**, both confirmed with full data on ext storage.

### Still open (enhancement, not a blocker)
Our own in-process installer still gets `0x80B2116F` from `sceAppInstUtilInstallByPackage`
(12 hypotheses eliminated). The app owns the download, queue, UI, console app, auto-start and
auto-recovery; only the final install call is delegated. See the INSTALL BLOCKER note for the
full investigation, the directory-install recipe, and two hard "do not repeat" hazards.

---

## [0.19.0] — 2026-07-26 · "Our own install engine — Elf Arsenal no longer required"

PKG MUTANT SHOP now downloads and installs packages **entirely by itself**. No DPI daemon, no
Elf Arsenal, nothing to wedge — the failure mode 0.18.0 recovered from can no longer occur,
because the component that wedged is gone from the path.

### What we learned first (from Arsenal's source, which is GPLv3)
Their code is **GPLv3+**, so none of it is used here — only the *mechanism*, which is fact:
API names, ABI struct layouts, error codes, and the order they try things in. Our implementation
is our own, and the app stays free of copyleft.

- **DPI v2 (:12800) installs nothing.** It is a thin HTTP shim that forwards to Arsenal's main
  process on `127.0.0.1:6969` and blocks with a **600-second** timeout. *That* is what the "wedge"
  always was — a synchronous install holding the socket, not a corrupted daemon.
- **Their reliability is a fallback ladder, not privilege.** Verified live: the same URL installed
  via `InstallByPackage` once, then minutes later fell back to download-then-`AppInstallPkg` (102s).
  `InstallByPackage` is unreliable for them too.
- **0x80B2116F was never a credentials problem.** Dumping ucred with `/api/cred?pid=N` showed our
  server is byte-identical to Arsenal's privileged processes — authid `0x4801000000000013`, caps all
  `ff`, attrs `00 00 00 80 …`. (So `attrs[3] |= 0x80` is right; setting `attrs[0]` too was a wrong
  guess, now reverted.)

### Added — the engine
- **`GET /api/engine/install-url?url=&name=&dest=`** — we stream the PKG to console storage
  ourselves, then register it with `sceAppInstUtilAppInstallPkg`, the one call that works from this
  process (it is what installs our own tile). Runs on a worker thread; the request returns at once.
- **`GET /api/engine/job`** — live `state / pct / done / total / content_id`, so progress is real
  bytes measured on the console instead of inferred.
- **`GET /api/engine/install-local?path=`** — install a PKG already on the console (USB → internal).
  Fully offline: no PC, no network, no Arsenal.
- **Resumable downloads.** `Range` resume, and the remote size is checked with a `HEAD` *before*
  resuming — asking for bytes past EOF on an already-complete file earns an HTTP 416 that looks
  exactly like a failure. 416 is also handled as "already complete".
- Content id is read from the PKG header (offset `0x40`), and `/data/…` is rewritten to
  `/user/data/…` because the install service is sandboxed and cannot see `/data`.
- Companion `dpi.mode` now defaults to **`pms`** and routes installs to our engine.

### Fixed
- **Premature "Ready to play".** The engine keeps the last job's result until a new one starts, so
  the queue read a *previous* completed job as its own and marked fresh downloads finished within
  seconds. Jobs now carry an **id**; the companion only trusts progress matching the id it was
  given, and waits for a free engine instead of being refused.
- Engine failures surface immediately instead of sitting through the generic 90-second
  "console isn't pulling" timeout.
- **Don't link `-lSceAppInstUtil`** in a payload: it isn't mapped into a freshly spawned process, so
  the loader drops the ELF before `main()` — the process appears in the list and executes nothing.
  The SPRX is loaded and its NIDs resolved at runtime instead.
- On-console builds self-replace via `/api/quit` (a reloaded ELF used to exit with "Already running",
  leaving the *old* code serving :8710).

### Verified on-device (FW 12.70, both titles deleted first)
- **DARK SOULS 7.27 GB** — downloaded in 183 s (~40 MB/s), installed `rc=0x00000000`,
  contentId `UP0700-CUSA08692_00-DARKSOULSHD00000`, and `CUSA08692` appeared in the installed list.
- **MARVEL 4.05 GB** — back-to-back immediately after, 103 s, `rc=0x00000000`,
  contentId `UP0102-CUSA04622_00-UMVC3FULLGAME000`. No reload between them.

### Known limits
- **Plain HTTP only.** That is what the companion serves; HTTPS would need a TLS stack in the
  payload. Remote `https://` sources are not supported by our engine yet.
- Installs do not appear in the PS5 download list (that is a property of `AppInstallPkg`).
- One package at a time; the queue serialises and waits.

---

## [0.18.0] — 2026-07-25 · "The install engine heals itself"

Multi-install is now **hands-free**. 0.17.3 could only *pause* on a wedged DPI daemon and ask for a manual
payload reload; the app now performs that reload itself, on both sides, and resumes the queue.

### How the reload was found (on-device research)
- The DPI's API is exactly **one endpoint** — `POST /api/install`. Everything else 404s, so there is **no
  in-band reset**. Relaunching the payload is the only real fix.
- **Payload Manager (pldmgr v0.5.0) is LAN-reachable on `:8084`** and exposes `/processes_list`,
  `/process_kill?pid=N`, `/list_payloads`, `/loadpayload:<path>` — the same route it uses itself
  (`"[PLDMGR] Sending ELF to local loader" / "Sent %zu bytes to loader"`, i.e. raw ELF bytes to elfldr).
- The ELF loader (`:3232`, and `:9021` on localhost) **only honours 127.0.0.1** — a push from the PC is
  accepted then RST. That is why the on-console path exists.
- **The install daemon is `dpiv2.elf`, a process of its own.** Relaunching elf-arsenal alone does *not*
  replace it: verified that a relaunch cycled `dpi.elf`/`ftpsrv`/`nanodns` while `dpiv2` kept its pid and
  `:12800` never dropped — the old instance still owns the port, so a fresh one cannot bind.
  **Killing it first is what makes the reload real.**
- The DPI v2 server is **compiled into `elf-arsenal.elf`** (its `dpiv2`/`12800`/`/api/install` strings live in
  the main binary; there is no separate payload). So while we depend on Arsenal, any DPI restart means
  relaunching Arsenal — a silent, DPI-only reload is not possible without our own engine.

### Added
- **Automatic DPI recovery — no user interaction.** `PayloadManager` + `Ps5Bridge.recover_dpi()`: kill the
  process holding the install port, wait for it to free, relaunch the payload, then wait for readiness.
  Measured **~4.6 s** end to end from the PC and **~3.6 s** on-console. `[VERIFIED]`
- **A readiness probe that tells the truth.** A bound port proves nothing — the wedged daemon still accepts
  sockets and still answers HEAD in ~14 ms. `dpi_install_ready()` POSTs a deliberately un-fetchable URL:
  healthy rejects it in ~15 ms (`{"res":"-1","error":"install failed"}`), wedged never answers. Nothing is
  ever queued or installed by the probe. `[VERIFIED]`
- **Self-healing queue.** Before each handoff the worker checks the install lane (~15 ms when healthy) and
  reloads only if genuinely wedged, so a healthy console is never disturbed. On a wedge mid-install it
  reloads and re-drives the same task once (`_healed`, so it can never loop). Holding the queue is now only
  the last resort, when recovery itself fails. `[VERIFIED]`
- **Works with the PC off** — the on-console ELF (v1.2.0) does the same thing for offline flows (USB →
  internal): `GET /api/dpi/reload`, `/api/dpi/status`, `/api/dpi/probe`. It drives Payload Manager over
  localhost, and falls back to pushing the payload straight into the localhost ELF loader. `[VERIFIED]`
- `POST|GET /api/dpi/reload` and `/api/dpi/status` on the companion (both verbs, so one UI call works whether
  the page is served by the PC or the PS5), plus a **⟳ button** in the queue dock for a manual reload.
- Queue shows a distinct amber-pulse **"reloading"** row while the engine is being restarted.

### Fixed
- **A dead engine no longer fakes a successful install.** `online = bridge.ping()` tests the *DPI port*, so a
  dead daemon sent the task down the offline branch and "installed" it as a **simulation** — reporting
  success for something that never happened. Now, if the console answers on FTP but the install port is
  dead, the engine is started first and the real path is used. Caught by the end-to-end test. `[VERIFIED]`
- **On-console upgrades actually take effect.** A newly loaded build used to hit "Already running" and exit,
  leaving the *old* code serving `:8710`. The new build now asks the running one to stand down
  (`/api/quit`) and takes the port. `[VERIFIED]`
- `/api/dpi/reload` hung until the client timed out: `do_POST` already reads the body, and the handler read
  it a second time, blocking on bytes that never come. `[VERIFIED]`

### Verified on-device
- Engine killed dead → install queued → **`reloading` → `submitting` → real `Downloading 4%` in 7.6 s**, no
  interaction, and not a simulation.
- Marvel (4.05 GB) installed → **`playable`**; Dark Souls (7.27 GB) queued behind it installed back-to-back.
- DPI measured **ready in 5 ms immediately after a 4 GB install** — the wedge is intermittent, not
  guaranteed, which is exactly why the cheap probe-then-heal design beats reloading after every install.

---

## [0.17.3] — 2026-07-25 · "Graceful wedge handling + reopen fix"

### Fixed
- **Panel reopen crash** — the 2-column restructure moved the action bar (`.dfoot`) inside `#dbody`; the next
  open's `innerHTML=""` destroyed it, so `openDrawer` threw on a null element and the panel wouldn't reopen
  until refresh. Now the action bar is rescued out of `#dbody` before the clear. `[VERIFIED]`
- **Multi-install wedge — now graceful.** Diagnosed on-device: the Elf Arsenal DPI daemon on 12.70 wedges its
  *install machinery* (AppInstUtil) after one heavy install and does NOT recover on its own — HEAD returns 400
  in 14 ms, but `POST /api/install` hangs 15 s. It can only be reset by reloading the payload on the PS5 (we
  can't do that from the PC: on-console loader + a blocked kernel-cred write). So instead of cascade-failing
  every queued game, a wedge now **HOLDS** the failed task + all pending installs (`Queue._pause_pending`) with
  a clear *"⏸ reload the DPI payload, then ▶ Start"* prompt (amber). Reload Elf Arsenal once, press ▶ Start,
  and the whole queue retries small-first. Verified: queue DS+Marvel → first wedges → both settle **held**, not
  errored. `[VERIFIED]`
- Confirmed the B5 health HEAD-probe is safe (doesn't wedge the daemon — it answers HEAD with a fast 400).

---

## [0.17.2] — 2026-07-25 · "Read the REAL console data + 2-column panel"

Second on-device test: Marvel installed, then the DPI daemon **wedged** and Dark Souls failed — B5 correctly
detected it ("daemon is wedged, reload"). Plus a batch of data/layout fixes driven by inspecting the real
`app.db`.

### Fixed / changed
- **Sizes/format/region are now REAL.** Pulled `app.db` and confirmed `tbl_contentinfo.size` == `AppInfoJson
  #_size` (it is NOT bogus — the small numbers were genuine stub installs). More importantly, most titles are
  **ShadowMount backups**, and the backup scan was only looking at one path. Now `console_backups` scans **all**
  real locations (`/data/homebrew`, `/mnt/ext0|ext1/homebrew`, `/mnt/usb0-7/homebrew`), so games show their real
  **backup size + `.ffpfsc` format** (Crash 4 22.6 GB, Crimson Desert 95 GB, DOOM 89 GB…). `console_apps` now
  also reads `contentId` → **region** and `contentLocation` → location. This was the real "not connected" bug.
- **2-column detail panel** (per request): **Details** (+ Base/Updates/DLC) on the **left**; **Actions bar
  (Install to ▾ · Install · ＋Queue · ⋯) + On console + Mods & Patches** on the **right**. Storage selector is
  now compact (in the right bar), panels are tighter (less wasted space), single→multi handled with a grid.
- **Wedge is now recoverable** — failed queue rows get a **↻ Retry** button (reload the DPI payload on the PS5,
  press retry; small-first ordering keeps it after anything running). The daemon wedging after one heavy
  install is an Elf Arsenal/12.70 limitation we can't remove, but the queue now pauses gracefully instead of
  dead-failing.
- Cards show real sizes again (reverted the earlier "—"). Both artifacts rebuilt to the Desktop; install path
  verified byte-perfect.

---

## [0.17.1] — 2026-07-25 · "Multi-install fix + card/panel polish (first on-device test)"

First real on-device test of the ELF: queue worked, Dark Souls installed perfectly, but Marvel (first in
the smallest-first queue) hit `HTTPError 400` immediately. Root-caused + fixed, plus the requested card/panel
fixes. `[VERIFIED]` on the PC; multi-install to re-test on-device.

### Fixed
- **Multi-install 400 (regression from 0.17.0's H1).** A freshly-loaded DPI daemon's FIRST POST transiently
  returns 400 before it settles; my H1 change had made *any* response non-retryable, so the first queued
  game failed. Corrected the retry policy in `_install_http`: **a 400 / rejection / connection-refused means
  nothing was queued → safe to retry**; only a read-timeout/reset (install maybe in flight) is not retried.
  So the cold-start 400 now recovers and both games install. `[VERIFIED build]`
- **Wrong size on cards.** `tbl_contentinfo.size` is a bogus metadata value; `build_library` now reports the
  real size only when we truly have it (PC pkg or ShadowMount backup) and `size_known:false` otherwise, so
  the UI shows "—" instead of a wrong number. PC-installable + backup titles show correct sizes.
- **Cards decluttered** — the 📍 install-location moved off the card (it stays in the panel).
- **Panel enriched** — added Version, File, Updates count, DLC count cells; nicer Format label (e.g.
  "Installed app" for DPI-installed titles instead of "—"); all values HTML-escaped.
- **PS5 panel perf** — dropped the CSS multi-column detail layout (`columns:2` reflowed slowly on PS5
  WebKit → single column) and deferred the FTP-backed cheat read until after the drawer opens, so the panel
  opens snappily.
- Both artifacts rebuilt + delivered to the Desktop (`PKG-MUTANT-SHOP.exe` / `.elf`); install path verified
  byte-perfect (Range @5 GiB = HTTP 206, identical).

---

## [0.17.0] — 2026-07-25 · "The overhaul — 12 batches, install path untouched"

A large, blueprint-driven feature overhaul, from a full audit and design pass. Every new
feature defaults to today's behavior; the working install handoff (`{"url":url}` → Elf Arsenal DPI v2 →
confirm) was never altered. All PC-verified against the live server; on-device items flagged. `[WIRED]`

### Added / changed
- **B1 Frontend safety + PS5-WebKit compat** — `hsl()` comma syntax (card covers render on WebKit), HTML-
  escaping everywhere (`esc()`), 2-arg `classList.toggle`→explicit, `closest`/`remove` polyfills,
  `-webkit-backdrop-filter`; **Circle/Back history sentinel** (closes the open panel, not the app);
  "Reinstall base" no-op fixed; install button no longer sticks on "Queued…". `[VERIFIED]`
- **B2 Install-pipeline hardening** — H1: `_install_http` retries **only** on connection-refused (a read
  timeout/response is final → no double-submit); L3 atomic JSON writes (`_atomic_write_json`); L4 FTP leak.
- **B3 UI foundation** — connection **LEDs** (Companion/LAN/FTP/DPI), dynamic version from `/api/health`,
  About section (real data), restyled ownership checkbox.
- **B4 Perf/cleanup/auto-rescan** — debounced **auto-rescan** (add/remove picked up ~10s), atomic scan swap
  (no 404 window), tunable 1 MiB serve buffer (Range still byte-perfect at 5 GiB), `sweep_temp_dbs`,
  `library_gen`→UI auto-refresh. `[VERIFIED]`
- **B5 Honest install-ready** — `dpi_live()` HEAD probe → `dpi_state` live/wedged/down (real daemon answers
  HEAD); DPI LED 3-state. `dpi_online` kept tied to reachability so a non-HEAD daemon can't false-blank it.
- **B6 Storage & devices** — `/api/devices` (PC drives + PS5 internal/ext0/ext1/usb0-7), the picker now
  offers **all destinations** (the "only ext1" fix); `/api/library/prepare` builds `Games/PS4|PS5`.
- **B7 Integrity** — `pkg_meta.pkg_completeness()` (pfs image fits EOF + high-entropy tail), pre-handoff
  verify (default **warn**, never blocks), `/api/verify/<key>`, ⋯-menu "Verify integrity".
- **B8 Smart queue** — install-now vs **＋Queue** (held), **▶ Start queue** releases smallest-first.
- **B9 Multi-PC federation** — `/api/federation` self-advertise + merged/deduped library (default off).
- **B10 i18n** — 7 languages (EN/ES/PT/FR/DE/JA + Arabic **RTL**), live switcher, localStorage persist.
- **B11 Multi-interface networking** — `companion_ip_for(target)` picks the PC IP on the console's subnet
  (Wi-Fi + Ethernet); single-NIC byte-identical.
- **B12 Clean reinstall** — ⋯-menu Clean-reinstall/Delete via a **guided** flow (gated `capabilities.uninstall`).
- **Ship — both artifacts rebuilt** so the actual workflow (load the ELF, run the EXE) carries everything:
  - **EXE** (PC companion): rebuilt with PyInstaller and installed to the Desktop path you run
    (`PKG-MUTANT-SHOP.NEW.exe`; old one kept as `.bak`), verified serving the new UI. `[VERIFIED]`
  - **ELF** (on-console): the **entire web UI is now embedded in the ELF** (`gen_web_bundle.py` →
    `web_bundle.h`, ~806 KB) and **self-extracted to `WEB_ROOT` on boot** — so loading the updated ELF
    alone delivers the new UI to the PS5, no separate push. On-console `/api/health` now reports `version`.
    Rebuilt via WSL prospero-clang; ELF 140 KB → 926 KB with the UI embedded, verified to contain
    `langSel`/`queueBtn`/`data-i18n`. `[WIRED]` (on-device load pending)
  - `deploy.py` WEB_ROOT gap also fixed as a fallback path.

### Needs on-device / your input
- **Load the rebuilt ELF** (`ps5-app/onconsole/pms-onconsole.elf`) via your payload loader — the PS5 gets the
  new UI automatically (self-extract). Then: on-device regression gate (Marvel + DS); confirm Circle-back +
  install-to-USB on the TV; B12 auto-uninstall NID/creds (else guided-manual stays). Deferred: B2 M2/M3/M4/L2,
  meta-cache, remote pre-fetch staging.

---

## [0.16.2] — 2026-07-24 · "Dark Souls 0% wasn't a decline — the DPI daemon was wedged"

Second real install after Marvel. **Ultimate Marvel vs. Capcom 3 installed fine; Dark Souls: Remastered stuck
at 0%** with *"Install engine declined it — is Elf Arsenal loaded on the PS5?"*. Chased it end-to-end with
direct probes from the companion box.

### Root cause (proven, not guessed)
- Companion side is healthy: after rescan the PKG serves **HTTP 200, 7,274,168,320 bytes, `Accept-Ranges: bytes`**.
- Port **12800 accepts TCP** (so `ping()`/"install-ready" go green), **but `POST /api/install` never returns a
  single byte** — the connection is aborted after a multi-second wait. The **DPI daemon is wedged**, almost
  certainly hung after the preceding Marvel install (payload instability on 12.70). `bridge.install()`'s three
  timeouts were being reported as "declined."
- **Not** the filename. Marvel is `…CUSA04622.ByCisso.pkg` (dots), Dark Souls was `[CUSA08692][BASE]-[DARKSOULS RE].pkg`
  (spaces + `[ ]`) — a plausible red herring, but the direct probe shows the daemon never even reads the URL. We
  percent-encode correctly (`_file_url`/registry key both `quote()`), and the clean-named file serves fine.

### Fixed
- **Failure reporting now names the actual mode.** `Queue._run` no longer prints the generic
  *"is Elf Arsenal loaded?"* for every failure. It inspects how `install()` failed and distinguishes three cases:
  **(a) port not listening** → "isn't listening — is the DPI payload loaded?"; **(b) port open but the request
  timed out/aborted** → *"accepts connections but isn't responding — the daemon is wedged; reload the DPI payload,
  then retry"* (the case we hit); **(c) a real rejection payload** → surfaces the daemon's `error`/`res`, plus a
  filename hint only if the URL actually contains spaces/brackets. Logs `[install] fail name=… url=… res=…` to the
  companion console and stores `detail` on the task. `[WIRED]`

### Housekeeping
- Renamed the library file to `DARKSOULS-REMASTERED-CUSA08692.pkg` (URL-safe). Not the fix, but good hygiene —
  artwork/title come from the PKG's param.sfo (`CUSA08692`), so the tile is unaffected.

### Known gap / next
- **`ping()` is not a liveness check.** A bare TCP connect passes against a wedged daemon, so the header can read
  "install-ready" when installs will actually time out. Next: make the DPI health check issue a real lightweight
  request with a short timeout (cached) so "install-ready" means *the daemon answered*, not *the port is open*. `[STUB]`

---

## [0.16.1] — 2026-07-22 · "The missing UserService init"

On-device re-test: shop payload showed *"Opening the shop…"* but the browser never opened; tile installer showed
no message at all. Root-caused both against the SDK samples.

### Fixed
- **Browser never launched** — `main.c` never called `sceUserServiceInitialize(0)` before
  `sceSystemServiceLaunchWebBrowser`. The SDK's `samples/browser/main.c` always initializes UserService first
  (in a constructor). Added it as the first line of `main()`, linked `-lSceUserService`, and now the launch
  **return code is reported on-screen** (`Browser launch failed: 0x…`) for both auto-open and the `open`
  command — so the next test is conclusive either way. `[WIRED]`
- **Tile installer showed nothing** — `sceUserServiceInitialize(0)` had been added *before* the first `notify()`;
  the SDK `install_app` sample never calls it, and it appears to fault in payload context before anything can
  display. Removed it; `notify("Installing tile…")` is now the literal first statement. `[WIRED]`

---

## [0.16.0] — 2026-07-22 · "Make it OPEN, and tell me why the tile failed"

On-device testing (real PS5, FW 12.70): the notification payload **works** (toast confirmed on TV). But the
tile "did nothing" and the shop never opened. This release fixes both — and makes the tile installer explain
itself instead of failing silently.

### Fixed
- **Shop now opens on load.** `ps5-app/payload/main.c` (`pkg-mutant-shop.elf`) previously only *notified* on
  load and opened the browser only on a TCP command. It now auto-calls `sceSystemServiceLaunchWebBrowser(PMS_URL)`
  on load (`PMS_AUTOOPEN=1`), so loading the payload opens PKG MUTANT SHOP. `[WIRED]` (browser-launch itself
  is what we're validating on-device).
- **Double title in toasts.** The companion prepended "PKG MUTANT SHOP\n" while the payload's toast already
  shows it as the sub-message → "PKG MUTANT SHOP / PKG MUTANT SHOP …". Companion now sends clean text
  (`web/index.html`: test-notify + mod ON/OFF). `[VERIFIED]` (companion side)

### Added — tile debugging
- **Self-diagnosing installer** `ps5-app/tile/tile-install.c` replaces the silent SDK sample. It now reports
  on-screen at every step: `Installing tile…` → checks the FTP'd files exist (`No app files on console — run
  deploy-tile.py first`, the #1 cause of "nothing happened") → exact `Register failed: 0x….` codes →
  `Tile installed!`. Links `-lSceNotification`. `[WIRED]`
- **Correct install ordering.** `deploy-tile.py` no longer pre-uploads the system-side `param.json`; the
  installer writes it to `/system_ex/app/PKGM00001/sce_sys/param.json` **after** registration (matches the SDK
  `install_app` sample's `test` target exactly). `[WIRED]`
- **"Open shop on PS5" button** (`web/index.html`) + `POST /api/open-ps5` + `Ps5Bridge.open_shop()` — sends the
  `open` command to the loaded payload so you can test the browser-launch from your PC without reloading a
  payload. `[WIRED]`

### Messages
- On-screen text is now customizable via build defines (`PMS_MSG_OPENING`, `PMS_MSG_READY`, `PMS_AUTOOPEN`).

---

## [0.15.0] — 2026-07-22 · "A dashboard TILE with your logo"

Built a real, fake-signed **homebrew app** — a tile on the PS5 dashboard with the user's logo that opens the
app. Same no-sudo WSL toolchain.

### Added — `ps5-app/tile/`
- **`PKGM00001/eboot.bin`** — fake-signed app executable that calls `sceSystemServiceLaunchWebBrowser(PMS_URL)`
  → launching the tile opens PKG MUTANT SHOP in the PS5 browser. Compiled dynamically, then ELF type patched
  `ET_DYN → ET_SCE_DYNAMIC` (what `make_fself` + the PS5 app loader require) and fake-signed with `make_fself.py`.
- **`install-tile-payload.elf`** — registers the app via `sceAppInstUtilAppInstallTitleDir` → `/user/app/` (the
  PS5's own installer; no etaHEN).
- **`PKGM00001/icon0.png`** (your logo) + `sce_sys/param.json[.system]` (titleName "PKG MUTANT SHOP").
- **`build-wsl.sh`** (reproducible, no sudo) and **`deploy-tile.py`** (FTPs the app files, `MTRW` +
  `/system_ex/app` + `/user/app`, then you run the payload via Payload Manager).

### Honest note
- Tile + icon + install path are solid (built on the SDK's `install_app` sample). The eboot's actual
  browser-launch is untested until first on-device run — if it needs a tweak, the toolchain is right here.

### Toolchain proven for both remaining asks
- Notifications ✅ (0.14) · dashboard tile ✅ (0.15) · **our own PKG installer** (`sceAppInstUtilInstallByPackage`)
  is the same pattern — next.

---

## [0.14.0] — 2026-07-22 · "THE .ELF IS REAL — compiled a PS5 payload"

After several attempts that concluded a PS5 `.elf` could not be compiled from Windows — **one was
compiled.** Using WSL2 and a user-space clang-18 (no sudo), the ps5-payload-dev SDK built the payload.

### Added
- **`ps5-app/payload/pkg-mutant-shop.elf`** — a real, compiled PS5 payload (113 KB, `ELF 64-bit LSB pie
  executable x86-64`, the correct PS5 loader format). On load it shows a notification and can open the app in the
  PS5 browser; it listens on **:9099** for `notify <text>` / `open <url>` from the companion.
- **`build-wsl.sh`** — fully reproducible, **NO-SUDO** build: fetches the ps5-payload-dev SDK + clang-18 `.deb`s
  into `~/` (via `apt-get download` + `dpkg-deb -x`), shims the SDK's `llvm-config` at the user-space clang, and
  compiles with `prospero-clang`. Verified: rebuilds the `.elf` cleanly from one command.
- Rewrote `main.c` against the **real** PS5 APIs discovered in the SDK samples: `sceNotificationSend(0xFE,…)`
  (a JSON toast) and `sceSystemServiceLaunchWebBrowser(url,0)`.

### How this unlocks the rest
The same toolchain now compiles the SDK's **`install_app`** sample (`sceAppInstUtilAppInstallTitleDir` →
`/user/app/` = a **dashboard tile**) and **PKG install** (`sceAppInstUtil…` = our own installer, no etaHEN).
Those are the next builds — the compile wall is gone.

---

## [0.13.0] — 2026-07-22 · "Install your way — Send-to-PS5 for the Debug Installer"

Explored the console to answer "how does an install work on Y2JB rather than etaHEN?". Payload Manager has
**no standalone ps5-dpi-v2** — DPI v2 there comes from **etaHEN**. So added a second install path that uses their
**proven Debug Package Installer**, no USB and no DPI host.

### Added
- **"📥 Send to PS5"** (drawer) — FTPs the PKG onto the console (`install.ftp_dest`, default `/data/pkg`) so you
  install it with **Debug Settings → Install Package Files**. New `via:"ftp"` install mode → `sendpkg` queue lane
  (reuses the mount FTP-push with real progress). No DPI, no USB. Verified: endpoint safe, button renders on PC PKGs.
- Config `install.ftp_dest`.

### Findings (from live FTP exploration of the console)
- Payload Manager repo payloads available: kstuff-lite, ShadowMountPlus, **websrv**, shsrv, ps5debug-NG, elfldr,
  ps5-app-dumper, **etaHEN**, … (no standalone ps5-dpi-v2). Autoload: nanodns + kstuff-lite 1.09 + ftpsrv + shadowmount.
- **`websrv` is available** → the realistic path to run our web UI as an on-console homebrew app (vs. the browser).
- Confirmed the app reads LIVE state: installed count tracked 45→44→45 as UMvC3 was deleted/reinstalled during testing.

---

## [0.12.0] — 2026-07-22 · "The payload + PS5 notifications"

### Added
- **PS5 payload source** (`ps5-app/payload/`: `main.c`, `Makefile`, `README.md`) — a real ELF for the
  ps5-payload-dev SDK that shows an **on-screen notification** on load and listens on **:9099** for the app to
  push live "mod X enabled on <game>" notifications (what CheatRunner does). Includes the mod-apply framework
  (memory-write engine) as the documented next step. **It is compiled with the SDK under WSL/Linux, not on
  Windows; the build steps are in the README.**
- **Companion → payload notifications** — `Ps5Bridge.notify()` + `POST /api/notify`. Toggling a mod now pushes a
  notification to the PS5 (once the payload is loaded); Settings has a **"📣 Test PS5 notification"** button.
  Verified: endpoint works (returns `ok:false` until the payload is running). Config `console.notify_port`.
- **Install-test readiness verified** — the new PC-folder PKG **Ultimate Marvel vs. Capcom 3 (CUSA04622)** parses
  correctly, shows "Not installed" (you deleted it from the console), and the companion serves the 4.05 GB file
  with Range/HEAD. Only blocker: launch the DPI host on the console.

---

## [0.11.0] — 2026-07-21 · "Real sizes, real storage, honest connection"

Fixed the bugs from the first real look at the running app. The numbers were wrong because the app.db `size`
is a bogus metadata value — the **real sizes are the ShadowMount `.ffpfsc` backup files** on the external SSD.

### Fixed (real data bugs)
- **Game sizes were wildly wrong** (NBA 2K26 showed 18 GB, Hogwarts 0.8 GB…). `Ps5Bridge.console_backups()` now
  scans the ShadowMount folders (`/mnt/ext1/homebrew` + configured paths) and uses the **real `.ffpfsc` file
  size**, matched by title-id. **Verified live: NBA 2K26 125 GB, Hogwarts 124 GB, Crimson Desert 95 GB, DOOM 89 GB.**
- **Storage was wrong** → now real used per drive from real sizes. **Verified: Internal 8 GB / Extended Storage
  843 GB (19 games).** (Total/free still shows "—" honestly — needs a partition query.)
- **"PS5 offline" was misleading** — it only checked the DPI port (install service, which you hadn't launched).
  `/api/health` now reports `ftp_online` + `dpi_online` separately; the pill shows **"connected (DPI off)"** when
  FTP works, so you know it's really talking to the console. **Verified: connected via FTP.**
- **Card badges clipped/overlapped** on narrow cards → `white-space:nowrap` + max-width + dark backdrop for
  readability over art. **Verified: consistent 70 px "Installed" badges.**

### Added
- **Format + PS5 location in the drawer** — e.g. Hogwarts → `124.1 GB · FFPFSC · 📂 /mnt/ext1/homebrew/[PS5]
  PPSA01593 - Hogwarts Legacy.ffpfsc`. Answers "what format / where on the PS5 / how big".
- **Open games folder** (Settings) — opens your `C:/PS4 PKG GAMES` folder in Explorer to manage the PC side.
- Settings now shows **FTP (data)** and **DPI v2 (install)** status separately.

### Researched (for the apply-engine)
- Studied `notmaj0r/CheatRunner`: cheats are **runtime memory writes** (game must be running), matched by
  **title-id + version** (`CUSA_01.09.json` exact → generic → wrong-version fallback), **crash-suspect flagging**,
  and an on-screen PS5 notification — all **on-console** behavior. This is the spec for our own apply-engine.

### Still on-console (needs the ps5-payload-dev SDK — can't compile from the laptop)
- The **native PS5 app / `.elf`**, **on-screen notifications**, and **mod *apply*** (writing game memory) all need
  a compiled payload. Mod *reading/parsing/matching* is done; *applying* is the payload. Offered as the next build.

---

## [0.10.0] — 2026-07-21 · "Truly connected — reads the whole console"

The app was reading detection from the PS5 but only *showing* the PC folder, so it looked local. Now it reads
and shows the **entire console** — every game, its real art, where it's installed, its real mods, and real
storage. All validated live.

### Added — the app now reads everything from the PS5
- **Console library merged in** (`build_library` + `Ps5Bridge.console_apps`, cached 30 s): all installed games
  from `tbl_contentinfo` are shown, merged/deduped with the PC folder. **Verified: 23 games.**
- **Real box art for every game** — lazy-fetches each game's `icon0.png` from the console (`icon0Info` path,
  `/user/appmeta/…` or `/user/app/…/sce_sys/…`) and serves it; initials render underneath as graceful fallback.
  **Verified: all 23 cards show real 512×512 art.** This fixes the "banner not visible" report.
- **Where each game is installed** — `contentLocation` → Internal SSD / Extended Storage, shown as a 📍 badge
  on the card + a chip + an "On console" section in the drawer. **Verified: Dark Souls @Internal, most @Extended.**
- **Real storage per drive** (`build_storage`) — used space = sum of installed game sizes by location.
  **Verified: Internal 8.0 GB (4 games), Extended 27.8 GB (19 games).** (Total/free still needs an on-device
  query → shown honestly, not faked.)
- **Real mod names + activation toggles** — the cheat reader now downloads + parses the JSON mod files to show
  actual mod names/types/version. **Verified: DOOM Eternal → Inf Ammo / Inf HP+Armor / Inf Jumps / Inf Upgrade
  Points; Dark Souls → 1 Hit Kill + Godmode / Max Souls.** Toggles are honest (see boundary below).
- **PS5 discovery** (`/api/discover` + Settings "🔍 Find PS5") — scans the LAN /24 for the FTP/DPI ports so you
  can re-point at the console when its IP changes; one click fills the IP field.
- **Decoupled from Cheat Runner** — the mod library path is configurable (`cheats.root`); we read the data
  files directly over FTP, never depending on the Cheat Runner service.

### Honest boundary
- **Mod *reading* is fully real; mod *applying* is not wired.** Applying a mod means writing its on/off bytes
  into the running game's memory — that needs our own on-console apply-engine (a payload), the same
  SDK/on-device class of work as the native app. The toggles read real and say so; the apply-engine is the
  next on-console build.

---

## [0.9.0] — 2026-07-21 · "LIVE on-device — real detection & real cheats"

First live session on the console (PS5 @ 10.0.0.99, ethernet). Confirmed real paths by FTP discovery and turned
two more assumptions into **validated, on-device-real** features.

### Fixed (real bugs found on hardware)
- **`doctor.py` crashed** on `lib.is_demo` (renamed to `is_empty` in 0.7.0 but not updated here). Fixed —
  found on the very first live run.
- **Install-detection was querying the wrong (PS4) tables** → returned nothing on PS5. The real PS5 12.70
  `app.db` lists installed titles in **`tbl_contentinfo`** (PS4-style `tbl_appinfo` lives in `appinfo.db`).
  Rewrote the query. **VERIFIED LIVE: `/api/installed` read 45 titles straight from the console over FTP,
  Dark Souls (CUSA08692) correctly detected.**
- **`shadowmount.scan_path` default was `/data/homebrew`, which doesn't exist on this console.** Your real
  ShadowMount config scans **`/mnt/ext1/homebrew`** (external SSD) — corrected in `config.json`.

### Added — Cheats are now REAL (Cheat Runner integration)
- `Ps5Bridge.console_cheats()` + `GET /api/cheats/<title_id>` read Cheat Runner over FTP
  (`/data/cheatrunner/cheats/{json,shn,mc4}` and `patches/{xml_orbis,xml_prospero,xml}`), matched by title-id.
- The drawer's **Cheats & Patches** tab now lists the real files for a title. **VERIFIED LIVE: Dark Souls shows
  `CUSA08692_01.03.json`, `.shn`, patch `CUSA08692.xml`; DOOM Eternal / Hogwarts / Castle Crashers also resolve.**

### On-device facts confirmed (see project memory + TOOLCHAIN.md)
- FTP `ftpsrv.elf` on :2121 ✓ · **DPI v2 :12800 NOT running** (launch Elf Arsenal / ps5-dpi-v2 for the PKG-install test).
- Tools present in `/data`: `pldmgr` (Payload Manager, with a payload repository), `shadowmount`,
  `GameCompressor`, `cheatrunner` (HTTP API on :9999).
- App DB confirmed at `/system_data/priv/mms/app.db`; cheat naming `<TITLEID>_<VER>.json`.

### Still pending (needs the DPI host launched)
- Real PKG **install** through DPI :12800 (the one remaining core path to validate live).

---

## [0.8.0] — 2026-07-21 · "Audit — correctness, performance & every-device views"

Full review pass. Found and fixed real defects (one that would have broken installs), sped things up, and made
the views correct on **PC, phone, and the PS5's TV** with controller navigation.

### Fixed (correctness — found in audit)
- **HTTP `HEAD` was unimplemented → 501.** Install clients (`AppInstUtil`) often HEAD the PKG for size /
  `Accept-Ranges` before downloading; a 501 could abort the install. Added `do_HEAD` for `/library/` and
  `/icon/` (returns `Content-Length` + `Accept-Ranges`, no body). `[VERIFIED: HEAD → 200]`
- **Suffix `Range` (`bytes=-N`) served the wrong bytes** (first N instead of last N). Fixed the range parser.
  `[VERIFIED: bytes=-100 → 206, exactly the last 100]`

### Performance
- **Fleet status pings all consoles in parallel** (was N × ping-timeout, serial). `[VERIFIED]`
- **Icons no longer re-extracted every scan** — skip when the cached PNG already exists (big win for large libraries).

### Device views (the ask: "perfect on each device")
- **Full responsive tiers:** phone (2-col ≤380px, wrapping header, full-width search, ≥42px touch targets),
  tablet, desktop, and **TV / PS5 (≥1600px): overscan-safe 44px padding + larger cards & text for couch viewing**;
  4K tier too. Verified at 375px and 1920×1080 — no horizontal overflow at any size. `[VERIFIED in-browser]`
- **Controller / keyboard navigation:** cards are focusable (`tabindex`/`role=button`, Enter/Space to open) and
  every interactive element gets a matte-yellow **`:focus-visible`** ring — usable with the DualSense on the TV.
- **Accessibility:** `role`/`aria-label` on cards, `alt` on the logo, focus rings only for keyboard/controller
  (pointer users stay clean).

### Polish
- **Clear-finished** button (🧹) in the queue dock (wired to `/api/queue/clear`).
- **Title count** in the source bar ("🎮 N titles").

---

## [0.7.0] — 2026-07-21 · "Honest Data — no fakes"

Stripped every piece of fabricated data. The app now shows **real data or an honest empty state — never
invented content**. Front, back, and settings all reflect actual state.

### Removed (fakes)
- **Demo/sample games** (Neon Drift, Hollow Sky, Ashfall, …) — deleted from both `web/index.html` and
  `server.py`. No more fabricated titles, ever. `[VERIFIED: only the real DARK SOULS shows]`
- **Fake storage reads** — the hardcoded drive usage bars (Internal 62%, USB0 28%, …) are gone. Storage now
  shows real free space **or an honest "—"** via `/api/storage` (`free:null` until an on-device query is
  wired — never a made-up number). `[VERIFIED]`
- **Fake "% free"** in the install drive-selector — replaced with plain drive labels.

### Changed
- Empty states are explicit and honest: opened as a bare file → "Interface preview. Run the companion…";
  companion up but no library → "No PKGs found. Set library.local_paths… then Rescan"; companion down →
  "Companion not reachable." The grid's "no match" text only appears when filters hide real titles.
- `/api/library` & `/api/rescan` now report `empty` (was `demo`); `is_demo` → `is_empty` throughout.
- New `/api/storage` (honest drive list) and `drives` config key.
- **Version fix:** `server.py` VERSION was stuck at 0.5.0 through the 0.6.0 work — now correctly **0.7.0**.

### Confirmed correct (PS4 PKG install location)
- PS4 PKGs go through the **INSTALL lane → DPI/AppInstUtil → real system install** (same as the Debug Package
  Installer), **not** the homebrew folder. They're detected by reading the console **app database**. Only PS5
  **backups** use the homebrew folder (MOUNT lane → ShadowMount). The two-lane split already does this right;
  nothing in the app pushes PKGs to homebrew.

---

## [0.6.0] — 2026-07-21 · "Correct for Y2JB 12.70 · Mount Lane"

Re-verified the app against the **current FW 12.70 toolchain**, made it tool-agnostic, and added the
ShadowMount "mount lane" for compressed backups.

### Fixed / corrected (accuracy for Y2JB 12.70)
- **DPI v2 is no longer described as etaHEN-only.** On 12.70 the `:12800/api/install` service comes from
  **Elf Arsenal** or **`ps5-dpi-v2`** (works FW 4.03–13.20+, handles the 11.00+ authid transparently); etaHEN
  is just one option on older FW. Corrected in `doctor.py`, `deploy.py`, and docs. The app already targeted the
  right standard API, so no behavior change — just correct guidance.
- **ELF-loader honesty:** the default loader is **localhost-only** (security) — PC→console ELF injection needs a
  LAN-enabled loader or on-console **Payload Manager**; FTP deploy + DPI install are unaffected. Documented.
- New **`TOOLCHAIN.md`** — authoritative map of the 12.70 stack (Y2JB/P2JB, autoloader / Payload
  Manager, DPI v2 hosts, Kstuff Lite, ShadowMountPlus, Game Compressor): roles, ports, boot order, and how the
  app uses each. Every assumption is a configurable default.

### Added — Mount lane (ShadowMount + Game Compressor)
- Scan recognizes **backup formats** `.ffpfsc` (Game-Compressor compressed PFS), `.exfat`, `.ffpkg`, `.ffpfs`
  as a **mount lane** (`lane:"mount"`, `platform:"BACKUP"`), distinct from PKG install. `[VERIFIED]`
- Installing a backup **FTPs it to the ShadowMount scan folder** (`shadowmount.scan_path`, default
  `/data/homebrew`) with real byte-progress via the FTP callback; ShadowMount auto-mounts on next scan. Offline
  → labeled simulation. `[VERIFIED routing; live FTP confirmed on-device]`
- UI: **MOUNT** badge, "Backup (ShadowMount)" section, **Mount** / **Mount backup** actions. `[VERIFIED in-browser]`
- Config: `shadowmount.scan_path`.

### Note
- etaHEN references in *older* changelog/roadmap entries are kept as dated history; TOOLCHAIN.md is the current
  source of truth.

---

## [0.5.0] — 2026-07-21 · "Native App & Branding"

Real branding + a real, no-toolchain path to an **installed PS5 app with your icon** (not the browser), plus
a self-bootstrapping PC app.

### Added
- **Official branding** — your icon + logo integrated. Header mark = your icon (128px, verified loaded),
  favicon (64px), 512×512 `icon0.png` for the PS5 app. Assets in `web/assets/`, referenced by **relative
  path** so they work when companion-served, from `file://`, and bundled on the console. `[VERIFIED in-browser]`
- **PS5 homebrew-app package** (`ps5-app/`): `homebrew.js` manifest + `sce_sys/icon0.png` — the "launch from
  the homebrew launcher with your icon, no browser" app (Route A). `[VERIFIED bundle]`
- **`deploy.py`** — real deploy tool. `payload <elf>` injects an ELF to the PS5 loader port (verified:
  4096-byte send/receive echo); `app` bundles icon+manifest+UI+assets, writes `config.js` with your companion
  URL, FTP-uploads to `/data/homebrew/PKG_MUTANT_SHOP/`; `check` tests loader+FTP reachability. `[VERIFIED sender+bundler]`
- **Configurable API base** — UI reads `window.PMS_API` (the PS5 app's `config.js`) so the on-console app
  talks to your companion box; same-origin when companion-served; demo from a bare file. `[VERIFIED]`
- **Self-bootstrapping PC app** — `bootstrap.cmd` auto-installs the only dependency (Python via winget) then
  launches; `build_exe.cmd` makes a single-file `PKG-MUTANT-SHOP.exe` (bundles Python+UI, zero deps on the
  target PC; companion is now `sys.frozen`-aware). **No DLLs/drivers needed — stdlib-only by design.**
- **BUILD-PS5-APP.md** — honest two-route guide (homebrew-launcher app now; system-dashboard fpkg via SDK).

### Honest boundary
- A *loadable* fake-signed **fpkg** (system games-dashboard icon, Route B) needs the **ps5-payload-dev SDK** +
  on-device signing — that compile step is not done from Windows, and no binary is claimed that was not built.
  Everything around it (icon, manifest, UI, bundling, FTP deploy, ELF injection) is built and tested; Route A
  needs no toolchain.

---

## [0.4.0] — 2026-07-21 · "Fleet, Parallel & Integrity"

No stubs left for later — the full PC↔PS5 pipeline is coded and the real online path was proven end-to-end
against a simulated console (byte progress climbed 0→100% → promote). PS5 WebKit compatibility hardened.

### Added
- **Multi-console fleet.** `consoles: [...]` in config; header **console picker** with **"All consoles"**
  fan-out. `Fleet`/`Ps5Bridge` per console; `/api/consoles` reports each console's online state. `[VERIFIED]`
- **Parallel queue** (`queue.max_parallel`, default 2) with **per-console serialization** (safe default; flip
  `allow_parallel_per_console` if firmware allows). Multiple workers, claim-under-lock. `[VERIFIED]`
- **Unified REAL progress.** Companion-hosted files → local byte-count; a **remote companion source** →
  polled via its `/api/served/<key>`. Proven end-to-end with a fake PS5 (real % + promote). `[VERIFIED]`
- **SHA-256 integrity.** `/api/hash/<key>` (streamed, cached by path+size+mtime); per-file **🔒 Verify**
  button in the drawer; hashes surface in manifests. `[VERIFIED wiring]`
- **Per-title manifest** `/api/manifest/<title_id>` (files, sizes, kinds, sha if computed). `[VERIFIED]`
- **`/api/served/<key>`** so any companion reports live bytes for remote-progress polling. `[VERIFIED]`
- **Settings** now shows the live **Sources** ranking (🟢/🔴 + latency) and **Consoles** fleet. `[VERIFIED]`
- **One-click launchers** `start.cmd` / `start.sh`; `sources.json.example`; **SETUP-REMOTE.md**
  (Tailscale "from anywhere", multi-console, mirrors).

### Fixed / hardened (before-they-happen)
- **Key-normalization bug:** `file_registry`/`install_key` are URL-quoted, but `_serve_library` + `served`/
  `hash` used the unquoted form → live LAN progress would read the wrong key on the console. Unified via
  `registry_key()`. Caught + fixed via the fake-PS5 test. `[VERIFIED]`
- **PS5 WebKit compatibility:** replaced `inset:0` → explicit `top/right/bottom/left`, and `aspect-ratio`
  → `padding-top` ratio box (confirmed 1.33 in-browser) so cards render on older console WebKit. `[VERIFIED]`
- Config loader now strips **all** `_`-prefixed doc keys (not just `_comment`).
- Deployment requirement surfaced by testing: companion must bind `0.0.0.0` (default) for the console to reach it.

---

## [0.3.0] — 2026-07-21 · "Source Engine"

Multi-mirror intelligence: the app auto-detects where each game can come from and picks the fastest reachable
source, with the LAN companion always available as a fallback. Full design in `ARCHITECTURE.md`.

### Added
- **`sources.py` — intelligent source engine.** Probes candidate mirrors concurrently (reachability + latency,
  30 s cache), ranks them (local → lowest-latency → priority), resolves the best URL for a file, fails over.
  Verified: ranks a live LAN host above a dead cloud host and picks it. `[VERIFIED]`
- **Source types** `local · lan · http · tunnel · cloud`, each `{name,type,base_url,priority,auth?}`.
- **Multi-source config** (`sources: []`) + an implicit always-on `companion-lan` mirror. `config.example.json`
  documents the shape (home-box-over-Tailscale + your-own-cloud examples). `[VERIFIED]`
- **Install uses selection:** `/api/install` resolves the best mirror holding the file (falls back to LAN host).
  Byte-accurate progress stays real for LAN-hosted files; remote pulls are honestly marked not-locally-measurable. `[VERIFIED]`
- **`/api/sources`** returns the live probe ranking + chosen best; UI source-bar shows "N sources · auto-picked <best>". `[VERIFIED]`
- **`ARCHITECTURE.md`** — full design: components, selection algorithm, parallel/background/multi-console, the
  three "from-anywhere" options with tradeoffs, security, reliability, future parking lot.

### Notes / honesty
- Recommended "from anywhere" path is a **home box + Tailscale** (private, free, no public hosting). Cloud/VPS
  sources are supported but must be **your own private storage of content you have the right to host** — the app
  ships no catalog and no piracy sources.
- Still ahead: fleet manager (multi-console), remote-source progress reporting, manifests + SHA-256, parallel-install
  limits on one console (to confirm on device).

---

## [0.2.0] — 2026-07-21 · "Real Metadata & Real Progress"

Ripped out the biggest stubs. Verified end-to-end on the laptop against a real retail PKG
(`DARK SOULS™: REMASTERED`, CUSA08692, 7.27 GB).

### Added — now REAL (was stub/heuristic)
- **`pkg_meta.py` — real PS4 PKG / param.sfo parser.** Reads TITLE, TITLE_ID, CONTENT_ID, CATEGORY,
  APP_VER/VERSION straight from the package. `[VERIFIED against CUSA08692]`
  - Category → type is now real: `gd`=base, `gp`=update, `ac`=dlc (no more filename guessing).
  - Region derived from CONTENT_ID prefix (UP→US, EP→EU, JP→JP, HP→ASIA, KP→KR).
- **Real box art.** Extracts `icon0.png` (entry 0x1200) from the PKG and serves it at `/icon/<title_id>.png`;
  UI shows the real cover (512×512 for Dark Souls), falling back to a generated tile only when absent. `[VERIFIED]`
- **Byte-accurate transfer progress.** The companion file host counts bytes as the PS5 pulls the PKG and
  reports true % — no more simulated bar while a real transfer is happening. `[REAL]` (measured), post-download
  promote step still shown as "installing on console" until the DPI status feed is captured. `[ON-DEVICE]`
- **Real install-detection.** `/api/installed` reads installed Title-IDs from the console app DB over **FTP + SQLite**
  (`installed_titles()`), merged with a local record of what we installed. Works when the console is online;
  DB path is configurable. `[WIRED]` (needs the console to confirm path/schema)
- **Source auto-read.** `/api/sources` + the UI's source bar show exactly *what folder is being read* and *the URL
  the PS5 pulls from* ("📂 Reading from … / ⬇ PS5 pulls from …"). `[VERIFIED]`
- **`doctor.py` test kit.** `python doctor.py` = full pre-flight (config, companion, console DPI/FTP reachability,
  real library list, install URL). `python doctor.py --install <TITLE_ID>` = drive one real install and watch live
  progress. `[VERIFIED]` (diagnostics), `[WIRED]` (install path)
- Config gains `ftp.port` (2121) and `console.app_db_path`. Default `library.local_paths` → `C:/PS4 PKG GAMES`.

### Removed / fixed
- Filename-heuristic metadata (`classify`, `pretty_name`, stub `region_of`) — replaced by real parse (kept only as
  a fallback if a file can't be parsed).
- **Fake cheat toggles** — the section no longer pretends to toggle runtime patches; shows an honest empty state
  until a real cheat backend is wired (Phase 4).
- **Bug:** UI treated same-origin API base `""` as falsy, forcing demo mode even when connected to the companion.
  Now uses explicit `API===null`. `[VERIFIED fixed]`

### Still honestly pending (needs the console)
- DPI v2 live *promote/status* endpoint (download % is real; the decrypt/promote tail is not observable yet).
- Confirm app-DB path/schema on PS5 for install-detection.
- Drive targeting (internal M.2 vs USB/ext) — how DPI selects the destination.

---

## [0.1.0] — 2026-07-21 · "Foundation"

First playable foundation. Everything renders and runs on the laptop today in **demo mode**; the console-specific paths are wired and clearly marked for tonight's FTP test.

### Added
- **Project scaffold** — self-contained web front-end (`web/index.html`) + stdlib-only Python companion (`companion/server.py`). No pip installs, no CDNs, no external assets (important: the UI must load inside the PS5's offline WebKit).
- **UI shell** in the MUTANT theme (black / matte-yellow / gray): library grid, game-detail drawer with **Base / Updates / DLC / Cheats** sections, install queue with progress bars, storage/drive selector, connection status, settings. `[VERIFIED]` (renders + interacts in demo mode)
- **Demo mode** — opening `web/index.html` directly (or running the companion with no library configured) shows a fully populated, clickable store with sample titles, so the interface is reviewable with zero hardware. `[VERIFIED]`
- **Companion service** (`companion/server.py`) — serves the UI and exposes `/api/health`, `/api/config`, `/api/library`, `/api/installed`, `/api/install`, `/api/queue`, `/api/queue/<id>/cancel`. `[VERIFIED]` (local), `[WIRED]` (console bridge)
- **PS5 bridge → etaHEN DPI v2** — real submit endpoint: `POST http://<ps5-ip>:12800/api/install` with `{"url": "..."}`, expecting `{"res":"0"}`. Legacy DPI v1 (port 9090 socket) adapter stubbed. `[WIRED]`
- **Source-agnostic library index** — scans user-configured local folders for `.pkg` and merges an optional `sources.json`. You supply your own content. `[VERIFIED]` (scan), `[STUB]` (metadata: filename heuristics for now)
- **Config system** — `config.example.json` → copy to `config.json`: PS5 IP, DPI mode/ports, ShadowMount port, library paths, companion host/port.
- **Docs** — `README.md` (architecture + honest feasibility notes) and `ROADMAP.md` (validated vs. needs-device).

### Needs on-device validation (moves to next version once captured on the console)
- DPI v2 **live progress/status** endpoint — install *submission* is confirmed; the percentage/state feed (`transferring → promoting → playable`) will be captured from the console's own WebUI network trace tonight. `[STUB]`
- **Install-state detection** — reading installed title-IDs from the system app database (path/format to confirm on device). `[STUB]`
- **`param.sfo` parsing** — real Title-ID / version / region / content-type from PKG headers, replacing filename heuristics. `[STUB]`

### Notes
- Base install path is the same for PS4 FPKGs on PS5 (via AppInstUtil/DPI). PS5-native game *backups* run through ShadowMountPlus (mount, not install) — tracked as a separate lane in ROADMAP. PS4-first, per plan.

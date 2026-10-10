# Contributing

## Reporting something

Open an issue. The two most useful things you can give are **what the app said** and **what the
console said**, word for word. An error code is worth more than a paragraph: `0x80B21104` with no
install row means out of space, `0x80990033` means the URL had no `.pkg` on the end, and they are
not interchangeable.

Say which console and firmware, which version of the app (the number under the logo), and where you
were looking at it — a console's own browser, a PC, or a phone. Those three behave differently and
"it doesn't work" rarely survives knowing which one you meant.

## Before you open a pull request

```bash
python tools/ready_check.py
```

That is every gate that can run on one machine — thirty tools, and all of them are fatal. None are
advisory, and none report a problem without failing.

The one exception is `test_fleet_two_consoles.py`, which needs **both** consoles awake and skips
itself otherwise. A gate that can skip silently cannot be relied on, so it is run by hand when both
consoles are on rather than being counted here.

Some gates read the owner's `Mutant Payloads & HomeBrews` folder (`gen_payload_catalog.py --check`,
`sync_payload_bins.py --check`). On a machine that has never had it, those describe your folder
rather than the code — rebuild them only if you actually changed a payload.

| Gate | What it refuses |
|---|---|
| `check_web.py` | a `web/index.html` whose single inline script does not parse, or uses something the console's browser does not have |
| `check_mobile.py` | a header child with no place in the phone layout — an unplaced one jumps to the front, not to where it was |
| `check_docs.py` | a README whose badges no longer match the build, a release note that does not say what changed, or a stamp point that has been reworded past the stamper |
| `i18n_report.py --check` | a string the app uses that is missing from any of the fifteen languages |
| `message_report.py --check` | a user-facing string that breaks the house style |
| `gen_payload_catalog.py --check` | a payload catalogue that has drifted from the folder it describes |
| `sync_payload_bins.py --check` | an embedded payload that is not the one on disk |
| `check_stale_exe.py` | an exe in the repo that is missing something it should bundle |
| `test_*.py` | the behaviour those tests pin |

## The rules this codebase actually holds itself to

**A check that passes when the code is broken is not a check.** When you add a test, break the code
it guards and watch it go red. There are several notes in this repo about checks that could not
fail and the bugs they let through.

**Measure, don't assume.** Claims in comments and docs here are expected to have a number behind
them. "Compression might help" became "a PKG gzips to 98.9%, so it will not". If you cannot measure
it, say that instead.

**Say what actually happened.** A message that says "Sent X" when nothing was sent is a bug. So is a
verdict the code cannot back — "installed" here means a `bgft.db` row *and* a full-size file on
disk, because both halves have been wrong on their own.

**Never guess a syscall signature.** Inferring one crashed a console. If the signature is not in
front of you, stop.

**Never update a console.** A system update removes the jailbreak this whole project depends on.
Nothing here starts, resumes or cancels a console-owned transfer.

## Working on the UI

`web/index.html` is one file with one inline `<script>`, and it is embedded in all three artifacts.
That makes it a single point of failure: one bad token blanks the app on every device while the
servers keep answering `200`. `check_web.py` parses it, but note what that does *not* catch — a
deleted element id. `$("#gone").onclick = ...` parses perfectly and throws at load. If you move or
rename anything in the markup, open the page in a real browser and check the console.

Two things the console browsers do differently, both learned the hard way:

- `classList.toggle(name, force)` — the second argument is ignored. Use `add`/`remove`.
- `:focus-visible` never fires there. The console moves focus with the D-pad and never reports the
  heuristic it waits for, so a focus style defined only on `:focus-visible` is invisible to a
  controller.

## Building

| Artifact | Command |
|---|---|
| `PKG-MUTANT-SHOP.exe` | `build_exe.cmd` (runs from `companion/`) |
| `PKG-MUTANT-SHOP.elf` | `bash ps5-app/onconsole/build-wsl.sh` |
| `PKG-MUTANT-SHOP-PS4.elf` | `bash ps4-app/build-all-wsl.sh` |

The PS4 one is the **whole chain** on purpose: the home-screen application carries its own copy of
the payload, so building only the ELF leaves a stale icon behind and nothing catches it.

Each console build needs its own SDK in WSL; the scripts say which and where they expect it.

## Licence

GPL-3.0. By contributing you agree your work is licensed the same way. Anything embedded that we did
not write is credited in [THIRD-PARTY-NOTICES.md](THIRD-PARTY-NOTICES.md) — if you add something,
add it there too.

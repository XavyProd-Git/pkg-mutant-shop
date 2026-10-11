# PS4 lane — integration check, with no console

The PS4 half of the app is mostly companion code, and the console it talks to is not always
reachable: a PS4's payload loader does not survive rest mode, so a suspend can take the hardware
away in the middle of a change. This is what stops that from meaning "untested".

```bash
python tools/ps4-itest/run.py ok            # a package that installs
python tools/ps4-itest/run.py psn-update    # a package the console stops part-way through
```

`fake_console.py` answers exactly what `ps4-app/onconsole/server_ps4.c` answers, and `run.py` points
a real `Ps5Bridge` at it and calls the real methods. Nothing starts a second app, writes to the
user's config, or opens a browser.

## What it actually proves

* two address settings become a two-console fleet, PS5 first
* a console's platform is learned from the console, and an unreachable one is not guessed at
* an unknown console id is refused instead of silently answered by "the first console"
* the PS4's installed games are read, with the shape the rest of the app expects
* an unreachable console gives up in about a second rather than waiting out FTP
* `addcont.db` is parsed with the PS4 schema, and already-installed is neither missed nor invented
* the library merges **every** console and records which one has each title
* content id, size and package type travel with the install request
* a running install answers as ACCEPTED inside the companion's 90-second window — the contract
  that, when it was wrong, would have reported every install longer than 90 seconds as a failure
* a failure carries the console's own words rather than a PS5 code table's guess
* the queue reads a PS4's verdict while transferring, does not mutate the shared byte counter,
  and leaves a PS5 exactly as it was (no extra round trip on its single accept loop)

## The fixtures

`make_fixtures.py` writes `app.db` and `addcont.db` (both gitignored, generated on first run).

**The schema is the real one**, taken from a PS4 on 13.52 — two per-user `tbl_appbrowse_<userid>`
tables, the key/value `tbl_appinfo` holding `APP_VER`, category `gd` for a game and `gdi` for a
system stub, and `addcont` with a `content_id` column. **The content is invented**: a real console's
databases carry the owner's library, user ids, entitlement ids and purchase dates, and none of that
belongs in a repository.

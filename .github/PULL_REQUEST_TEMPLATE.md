## What this changes

<!-- One paragraph. What behaviour is different afterwards? -->

## Why

<!-- What went wrong, or what was missing. If a console told you something, quote it. -->

## How it was checked

<!-- Delete what does not apply. "It should work" is not a line in this list. -->

- [ ] `python tools/ready_check.py` is green
- [ ] Exercised on hardware: PS5 / PS4 / neither
- [ ] A test pins the new behaviour, and was **perturbed to red** to prove it can fail
- [ ] Every user-facing string is in all fifteen languages (`python tools/i18n_report.py --check`)
- [ ] If a console-side route was added, it is in `route_changes_state()`
- [ ] If an artifact changed, the right build was run
      (`build_exe.cmd`, `ps5-app/onconsole/build-wsl.sh`, **`ps4-app/build-all-wsl.sh`** — the whole
      chain, or the PS4 home-screen icon keeps the previous payload)

## Anything that is now true but not obvious

<!-- The thing the next person would otherwise have to rediscover. -->

# -*- mode: python ; coding: utf-8 -*-
#
# THE UI GATE AND THE VERSION STAMP RUN HERE, not just in the ELF build.
#
# Both artifacts embed web/index.html. build-wsl.sh runs tools/check_web.py and
# tools/stamp_version.py before it bundles the UI into the ELF; NOTHING ran them before the exe
# bundled it. So building only the exe could ship a UI whose script does not parse - which blanks
# the app on every device while the server keeps answering 200 - or a stale APP_VERSION that makes
# the page lie about which build the user is looking at.
#
# It happened to be safe until now only because the ELF was always built first by hand. Ordering is
# not a safeguard; this is.
import os
import subprocess
import sys

_HERE = os.path.dirname(os.path.abspath(SPEC))
_ROOT = os.path.dirname(_HERE)


def _gate(script, fatal, args=()):
    path = os.path.join(_ROOT, "tools", script)
    if not os.path.exists(path):
        # A missing gate is a build with one less guard, not a build that is fine. Say so loudly.
        if fatal:
            raise SystemExit("ABORT: tools/%s is missing - not building without its check." % script)
        print("spec: %s missing - skipping" % script)
        return
    rc = subprocess.call([sys.executable, path] + list(args), cwd=_ROOT)
    if rc != 0:
        if fatal:
            raise SystemExit("ABORT: %s failed (rc=%d) - not building an exe that ships this."
                             % (script, rc))
        print("spec: warning - %s returned %d" % (script, rc))


# EVERY GATE IS FATAL. stamp_version used to be a warning, so a version drift scrolled past in
# the middle of a long build and two artifacts labelled the same number differed. The three
# gates below it were wired into NO build at all - test_storage_tiles guards a regression that
# came back once, and the i18n and message gates were cited as passing by the changelog while
# nothing ran them. A gate that only runs when someone remembers is not a gate.
_gate("stamp_version.py", True)            # APP_VERSION / SHOP_VERSION / homebrew.js == server.py VERSION
_gate("check_web.py", True)                # refuse to package a UI whose script will not parse
_gate("i18n_report.py", True, ["--check"])  # every language covers every key the app uses
_gate("message_report.py", True, ["--check"])  # every user-facing sentence is on house style
_gate("test_storage_tiles.py", True)       # the M.2 duplicate tile must not come back a third time
# A name that does not exist is a 500 on the route that uses it, and this module is nine thousand
# lines of handlers that only run when someone presses the thing. Both "reload the install engine"
# controls shipped answering 500 with a NameError because nothing ever pressed them in a test.
_gate("lint_python.py", True, ["--check"])

# THE SHIPPED CHEAT ARCHIVE, GATED ON BEING CURRENT. assets/cheats-library.zip is a datas entry, so
# PyInstaller fails loudly when it is MISSING and ships a STALE one without a word - and stale is the
# worse failure, because every PS4 that asks this PC for cheats then receives an old library and
# nothing says so. Repacking is cheap and deterministic, so this repacks once and only then insists:
# the build dies only if the repack did not fix it. (The rule three lines up: a gate that only runs
# when someone remembers is not a gate.)
_packer = os.path.join(_ROOT, "tools", "pack_cheats.py")
if not os.path.exists(_packer):
    raise SystemExit("ABORT: tools/pack_cheats.py is missing - cannot verify the shipped cheat library.")
if subprocess.call([sys.executable, _packer, "--check"], cwd=_ROOT) != 0:
    print("spec: the shipped cheat library is out of date - repacking")
    subprocess.call([sys.executable, _packer], cwd=_ROOT)
    _gate("pack_cheats.py", True, ["--check"])


a = Analysis(
    ['server.py'],
    pathex=[],
    binaries=[],
    # The PS4 dashboard app travels with the exe: a PS4 has no other way to get the shop onto
    # its home screen, and the companion installs it the first time it sees a PS4 without it.
    # Absent at build time is not fatal - the app simply has nothing to offer.
    # Forward slashes on purpose: PyInstaller accepts them and a backslash before 't' in
    # 'tile-pkg' is a tab in any string that is not raw.
    datas=[('../web', 'web'),
           ('../ps4-app/tile-pkg/IV0000-PKGM00001_00-PKGMUTANTSHOP001.pkg', 'ps4-tile'),
           # The PS4 payload, so this exe can bring a PS4's shop up with nobody pressing
           # the icon. A GoldHEN payload does not survive a reboot and there is no autoload
           # folder on that console, so without this the only route back is the icon.
           ('../ps4-app/onconsole/PKG-MUTANT-SHOP-PS4.elf', 'ps4-elf'),
           # THE CHEAT LIBRARY, as ONE archive (tools/pack_cheats.py). A PS5 carries its own copy
           # inside its ELF; a PS4 cannot, so it takes the library from the PC - and until this
           # line the shipped exe had none to give, which made cheats a repo-only feature on that
           # console. One file and not 6275, because the one-file build unpacks every bundled data
           # file into %TEMP% on every launch; server.py expands it beside the exe exactly once.
           ('../assets/cheats-library.zip', 'cheats-pack'),
           # THE VERSIONS THIS APP HAS PROVEN, keyed by sha256. Without this file a packaged build
           # answers {} from known_versions(), so every payload that writes no version into its
           # binary or its filename - ftpsrv x2, nanodns x2, OnionHEN, ShadowMountPlus - comes out
           # of a live rescan with a blank version. A blank version cannot be compared against an
           # upstream tag, so those six could never be offered an update by the shipped exe, which
           # is the only build anybody runs. The baked catalogue carried the answer; a rescan threw
           # it away.
           ('../assets/payloads/known-versions.json', 'payload-versions')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # numpy is pulled in by Pillow's hook and imported by nothing here: 26.5 MB unpacked into
    # %TEMP% on EVERY launch of the one-file exe (55 MB total, half of it numpy). Pillow does
    # not need it for anything the companion does (open, resize, save WebP).
    excludes=['numpy'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='PKG-MUTANT-SHOP',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    # upx=True only did something on a machine with UPX installed - a second machine-dependent
    # variable in an exe whose bytes should depend on the source alone. UPX is not a build
    # dependency of this project, so the flag is off everywhere.
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['..\\web\\assets\\app.ico'],
)

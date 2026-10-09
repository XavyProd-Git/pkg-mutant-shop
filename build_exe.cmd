@echo off
REM Build a single-file PKG-MUTANT-SHOP.exe that bundles Python + the web UI.
REM The resulting exe needs NOTHING on the target PC (no Python, no DLLs, no drivers).
setlocal
cd /d "%~dp0companion"
where python >nul 2>nul || (echo Python required to BUILD the exe ^(the exe itself needs nothing^). & pause & exit /b 1)
echo Installing PyInstaller 6.21.0 (pinned - one-time)...
REM PINNED, not --upgrade. An unpinned upgrade meant the next build could silently pick up a
REM newer major with a different bootloader and different antivirus behaviour, so two exes
REM labelled the same version could differ in ways unrelated to the source. 6.21.0 is the
REM release the shipping exe was built with.
python -m pip install pyinstaller==6.21.0 || (echo pip failed & pause & exit /b 1)
echo Building...
REM BUILD FROM THE SPEC, not from flags. The spec carries the UI gate and the version stamp
REM (see its header); this command line did not, so building the exe this way could package a
REM UI whose script does not parse - which blanks the app on every device while the server
REM still answers 200 - or a stale APP_VERSION. The flags here had also drifted out of step
REM with the spec, so there were effectively two different exes depending on how you built.
python -m PyInstaller --noconfirm PKG-MUTANT-SHOP.spec || (echo BUILD FAILED - see the error above & pause & exit /b 1)
echo.
echo Done -> companion\dist\PKG-MUTANT-SHOP.exe
echo Put config.json next to the exe (copy from companion\config.example.json) and run it.
pause

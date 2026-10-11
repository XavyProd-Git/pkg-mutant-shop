@echo off
REM PKG MUTANT SHOP - self-bootstrapping launcher (Windows).
REM Ensures the ONLY dependency (Python) is present, then starts the app.
REM The companion is stdlib-only Python: no DLLs, no drivers, no pip packages required.
setlocal
where python >nul 2>nul
if %errorlevel%==0 goto haspy

echo Python not found - attempting automatic install...
where winget >nul 2>nul
if %errorlevel%==0 (
  winget install -e --id Python.Python.3.12 --silent --accept-package-agreements --accept-source-agreements
  echo.
  echo Python installed. CLOSE this window and run bootstrap.cmd again ^(so PATH refreshes^).
  pause
  exit /b 0
) else (
  echo Could not auto-install ^(winget missing^).
  echo Please install Python 3.8+ from https://www.python.org/downloads/  ^(tick "Add to PATH"^), then rerun.
  pause
  exit /b 1
)

:haspy
for /f "tokens=2" %%v in ('python --version 2^>^&1') do echo Using Python %%v
call "%~dp0start.cmd"

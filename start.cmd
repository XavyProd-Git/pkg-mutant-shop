@echo off
REM PKG MUTANT SHOP - one-click launcher (Windows)
cd /d "%~dp0companion"
if not exist config.json (
  echo Creating config.json from template - edit ps5_ip + library.local_paths, then rerun.
  copy /y config.example.json config.json >nul
)
echo Starting PKG MUTANT SHOP companion...
start "" cmd /c "timeout /t 2 >nul & start http://localhost:8710"
python server.py
pause

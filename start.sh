#!/usr/bin/env bash
# PKG MUTANT SHOP - one-click launcher (Linux / macOS / home-box)
cd "$(dirname "$0")/companion" || exit 1
if [ ! -f config.json ]; then
  echo "Creating config.json from template - edit ps5_ip + library.local_paths, then rerun."
  cp config.example.json config.json
fi
echo "Starting PKG MUTANT SHOP companion..."
( sleep 2; (xdg-open http://localhost:8710 || open http://localhost:8710) >/dev/null 2>&1 ) &
exec python3 server.py

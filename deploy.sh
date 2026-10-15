#!/usr/bin/env bash
# Copy the mod into the TF3 user mods folder (game must be restarted to pick up new files).
# Thin wrapper around tf3planner/control/deploy.py: the target folder
# <Steam>/userdata/<account>/3493540/local/mods is discovered by tf3planner/config.py
# (override: TF3P_USERDATA=/path/to/local ./deploy.sh). _content.json is regenerated from the
# actual files (the game only loads files listed there).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
PY="$ROOT/.venv/Scripts/python.exe"
[ -x "$PY" ] || PY="$ROOT/.venv/bin/python"
[ -x "$PY" ] || PY="$(command -v python3 || command -v python)"
cd "$ROOT"
exec "$PY" -X utf8 -m tf3planner.control.deploy "$@"

"""Filesystem locations used by the control center.

Machine-specific locations (Steam user folder, game install, planner data folder) are discovered in
tf3planner/config.py (registry + libraryfolders.vdf, env / settings.json overrides).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from .. import config

ROOT = config.CODE_ROOT                 # project root (source) or the bundle's _internal (built app)
DATA = config.data_dir()
LOGS = DATA / "logs"
SETTINGS = DATA / "settings.json"
JOBS = DATA / "jobs.json"
SERVER_LOG = DATA / "web_server.log"

USERDATA = config.userdata_dir()        # <Steam>/userdata/<account>/3493540/local
EXPORT_LUA = USERDATA / "tf3_planner" / "export.lua"
GAME_STDOUT = USERDATA / "crash_dump" / "stdout.txt"
MODS_DIR = USERDATA / "mods"
MOD_NAME = "tf3_planner_1"
MOD_SRC = ROOT / "mod" / MOD_NAME
MOD_DST = MODS_DIR / MOD_NAME

EXPORT_JSON = DATA / "export.json"
PLAN_JSON = DATA / "plan.json"
ROUTES_JSON = DATA / "routes.json"
CALIBRATION_JSON = DATA / "calibration.json"
DOSSIERS_JSON = DATA / "dossiers.json"

PKG = ROOT / "tf3planner"
DEFAULT_PORT = 8010

GAME_EXE = "TransportFever3.exe"


def venv_python(windowless: bool = False) -> str:
    """Python of the project venv (python.exe, or pythonw.exe for windowless launch).

    Built app (PyInstaller): the bundled executables understand `-X utf8 -u -m module args`
    (packaging/entry.py), so callers keep building python-style command lines."""
    if config.FROZEN:
        exe = config.APP_DIR / ("TF3Planner.exe" if windowless else "tf3planner-cli.exe")
        return str(exe if exe.exists() else Path(sys.executable))
    scripts = ROOT / ".venv" / "Scripts"
    exe = scripts / ("pythonw.exe" if windowless else "python.exe")
    if exe.exists():
        return str(exe)
    # fall back to the interpreter we run in
    cur = Path(sys.executable)
    if windowless:
        w = cur.with_name("pythonw.exe")
        if w.exists():
            return str(w)
    elif cur.name.lower() == "pythonw.exe":
        p = cur.with_name("python.exe")
        if p.exists():
            return str(p)
    return str(cur)


def find_bash() -> str | None:
    for cand in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe"):
        if Path(cand).exists():
            return cand
    return shutil.which("bash")


NO_WINDOW = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW
DETACHED = 0x00000008 if os.name == "nt" else 0           # DETACHED_PROCESS
NEW_GROUP = 0x00000200 if os.name == "nt" else 0          # CREATE_NEW_PROCESS_GROUP

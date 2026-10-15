# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec: one-folder build of the TF3 Planner desktop app -> dist/TF3Planner/
#
#   packaging\build.ps1                (creates .venv-build, installs requirements, runs this spec)
#   .venv-build\Scripts\pyinstaller --noconfirm --clean packaging\tf3planner.spec
#
# Output: dist/TF3Planner/TF3Planner.exe (windowed launcher), tf3planner-cli.exe (console, jobs),
# _internal/ (Python runtime, libraries, tf3planner sources + web UI, the in-game mod, tools).
# Nothing from data/ (player maps, plans, game-derived recipes) is bundled.
import os
from pathlib import Path

from PyInstaller.utils.hooks import collect_submodules

ROOT = Path(SPECPATH).resolve().parent
PKG = ROOT / "tf3planner"
MOD = ROOT / "mod" / "tf3_planner_1"
ICON = PKG / "control" / "tf3planner.ico"

SKIP_DIRS = {"__pycache__", ".pytest_cache"}
SKIP_FILES = {"mod.io_fileid.txt"}          # never ship the author's mod.io link file


def tree(src: Path, dest: str):
    """(file, dest_dir) pairs for every file under src (sources stay readable on disk: the control
    center checks job modules by file, the mod is copied into the game's mods folder)."""
    out = []
    for dirpath, dirnames, filenames in os.walk(src):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        rel = Path(dirpath).relative_to(src)
        for f in filenames:
            if f in SKIP_FILES or f.endswith((".pyc", ".log")):
                continue
            out.append((str(Path(dirpath) / f), str(Path(dest) / rel)))
    return out


datas = tree(PKG, "tf3planner") + tree(MOD, "mod/tf3_planner_1")
datas.append((str(ROOT / "packaging" / "bundle" / "TF3 Planner.vbs"), "."))

# OR-Tools loads its native DLLs by path from ortools/.libs (WinDLL in ortools/__init__.py):
# PyInstaller does not see them as dependencies, so copy them explicitly.
binaries = []
try:
    import importlib.util
    _ort = Path(list(importlib.util.find_spec("ortools").submodule_search_locations)[0])
    binaries += [(str(p), "ortools/.libs") for p in (_ort / ".libs").glob("*.dll")]
except Exception:   # ortools is optional
    pass

hiddenimports = (
    collect_submodules("tf3planner")
    + collect_submodules("uvicorn")
    + ["extract_recipes", "gen_schema", "lupa.lua52", "elkai"]
    + ["ortools.constraint_solver.pywrapcp", "ortools.constraint_solver.routing_enums_pb2"]
)

a = Analysis(
    [str(ROOT / "packaging" / "entry.py")],
    pathex=[str(ROOT), str(ROOT / "tools")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "pytest", "IPython", "matplotlib", "pandas", "PyQt5", "PySide6", "notebook"],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

# python interpreter options baked into both executables (same as `python -X utf8 -u`)
OPTIONS = [("X utf8", None, "OPTION"), ("u", None, "OPTION")]

exe_gui = EXE(
    pyz, a.scripts, OPTIONS, [],
    exclude_binaries=True,
    name="TF3Planner",
    console=False,
    icon=str(ICON),
    upx=False,
)
exe_cli = EXE(
    pyz, a.scripts, OPTIONS, [],
    exclude_binaries=True,
    name="tf3planner-cli",
    console=True,
    icon=str(ICON),
    upx=False,
)
coll = COLLECT(
    exe_gui, exe_cli,
    a.binaries, a.datas,
    strip=False,
    upx=False,
    name="TF3Planner",
)

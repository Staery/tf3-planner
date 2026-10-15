"""Path discovery for TF3 Planner (one place for every machine-specific location).

Resolution order for each location (first hit wins):

  user data folder  <Steam>/userdata/<account>/3493540/local  (export.lua, mods, crash_dump/stdout.txt)
      1. env  TF3P_USERDATA
      2. data/settings.json  "userdata_dir"
      3. auto: every Steam install (registry HKCU\\Software\\Valve\\Steam SteamPath,
         HKLM\\SOFTWARE\\WOW6432Node\\Valve\\Steam InstallPath, default folders) ->
         userdata/*/3493540/local; several accounts -> the most recently modified one
  game install      <library>/steamapps/common/Transport Fever 3  (read-only: recipes / schema tools)
      1. env  TF3P_GAME
      2. data/settings.json  "game_dir"
      3. auto: Steam libraries from steamapps/libraryfolders.vdf + appmanifest_3493540.acf
  planner data      data/ of the project (source checkout) or %LOCALAPPDATA%\\TF3Planner\\data (built app)
      1. env  TF3P_DATA
      2. built app: <exe folder>/data if it exists (portable mode), else %LOCALAPPDATA%\\TF3Planner\\data
      3. source checkout: <project>/data

The packaged launcher also accepts --userdata / --game / --data (they set the env variables, so
every child process sees them). `python -m tf3planner.config` prints what was found;
`--set-userdata PATH` / `--set-game PATH` store an override in settings.json ("" clears it).

Everything here is cheap and has no side effects at import time except reading the registry and a
few directory listings once (results are cached).
"""
from __future__ import annotations

import functools
import json
import os
import re
import sys
from pathlib import Path

APP_ID = "3493540"                  # Transport Fever 3 on Steam
MODIO_GAME_ID = "10640"             # Transport Fever 3 on mod.io
GAME_FOLDER = "Transport Fever 3"
APP_NAME = "TF3Planner"
APP_VERSION = "0.9.0"               # desktop app; bump together with packaging/tf3planner.iss

FROZEN = bool(getattr(sys, "frozen", False))
# code + bundled resources (web/static, mod sources): the project root, or PyInstaller's _internal
CODE_ROOT = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent.parent)) if FROZEN \
    else Path(__file__).resolve().parent.parent
# folder of the program the user starts (TF3Planner.exe), or the project root
APP_DIR = Path(sys.executable).resolve().parent if FROZEN else CODE_ROOT

ENV_USERDATA, ENV_GAME, ENV_DATA = "TF3P_USERDATA", "TF3P_GAME", "TF3P_DATA"


# ---------------------------------------------------------------------------- planner data

def _local_appdata() -> Path:
    base = os.environ.get("LOCALAPPDATA") or os.path.join(os.path.expanduser("~"), "AppData", "Local")
    return Path(base) / APP_NAME


@functools.lru_cache(maxsize=None)
def data_dir() -> Path:
    env = os.environ.get(ENV_DATA)
    if env:
        return Path(env).expanduser()
    if FROZEN:
        portable = APP_DIR / "data"
        return portable if portable.is_dir() else _local_appdata() / "data"
    return CODE_ROOT / "data"


def settings_file() -> Path:
    return data_dir() / "settings.json"


def _settings_raw() -> dict:
    try:
        raw = json.loads(settings_file().read_text(encoding="utf-8"))
        return raw if isinstance(raw, dict) else {}
    except (OSError, ValueError):
        return {}


def save_override(key: str, value: str | None) -> Path:
    """Store "userdata_dir" / "game_dir" in settings.json (keeps all other keys)."""
    raw = _settings_raw()
    if value:
        raw[key] = str(Path(value).expanduser())
    else:
        raw.pop(key, None)
    f = settings_file()
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(json.dumps(raw, ensure_ascii=False, indent=1), encoding="utf-8")
    userdata_dir.cache_clear()
    game_dir.cache_clear()
    return f


# ---------------------------------------------------------------------------- Steam

def _reg_value(root, key: str, name: str) -> str | None:
    try:
        import winreg
        with winreg.OpenKey(root, key) as k:
            v, _ = winreg.QueryValueEx(k, name)
            return str(v) if v else None
    except (OSError, ImportError):
        return None


@functools.lru_cache(maxsize=None)
def steam_roots() -> tuple[Path, ...]:
    """Steam client installs (usually one), registry first."""
    cands: list[str] = []
    try:
        import winreg
        cands += [
            _reg_value(winreg.HKEY_CURRENT_USER, r"Software\Valve\Steam", "SteamPath"),
            _reg_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\WOW6432Node\Valve\Steam", "InstallPath"),
            _reg_value(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Valve\Steam", "InstallPath"),
        ]
    except ImportError:
        pass
    for env in ("ProgramFiles(x86)", "ProgramFiles"):
        if os.environ.get(env):
            cands.append(os.path.join(os.environ[env], "Steam"))
    cands += [os.path.expanduser("~/.steam/steam"), os.path.expanduser("~/.local/share/Steam")]
    out: list[Path] = []
    seen: set[str] = set()
    for c in cands:
        if not c:
            continue
        p = Path(c)
        try:
            p = p.resolve()
        except OSError:
            continue
        key = os.path.normcase(str(p))
        if key not in seen and p.is_dir():
            seen.add(key)
            out.append(p)
    return tuple(out)


def _vdf_paths(text: str) -> list[str]:
    """Library paths from libraryfolders.vdf (new format: "path" "X"; old format: "1" "X")."""
    paths = re.findall(r'"path"\s+"([^"]+)"', text)
    if not paths:
        paths = re.findall(r'^\s*"\d+"\s+"([^"]+)"', text, re.M)
    return [p.replace("\\\\", "\\") for p in paths]


@functools.lru_cache(maxsize=None)
def steam_libraries() -> tuple[Path, ...]:
    libs: list[Path] = []
    seen: set[str] = set()

    def add(p: Path):
        k = os.path.normcase(str(p))
        if k not in seen and (p / "steamapps").is_dir():
            seen.add(k)
            libs.append(p)

    for root in steam_roots():
        add(root)
        vdf = root / "steamapps" / "libraryfolders.vdf"
        try:
            for p in _vdf_paths(vdf.read_text(encoding="utf-8", errors="replace")):
                add(Path(p))
        except OSError:
            pass
    return tuple(libs)


def _installdir(lib: Path) -> str | None:
    acf = lib / "steamapps" / f"appmanifest_{APP_ID}.acf"
    try:
        m = re.search(r'"installdir"\s+"([^"]+)"', acf.read_text(encoding="utf-8", errors="replace"))
        return m.group(1) if m else None
    except OSError:
        return None


def _looks_like_game(p: Path) -> bool:
    return (p / "base").is_dir() and ((p / "TransportFever3.exe").exists() or (p / "api").is_dir())


def discover_game_dir() -> Path | None:
    libs = steam_libraries()
    for lib in libs:                        # libraries that have the app manifest first
        inst = _installdir(lib)
        if inst and _looks_like_game(lib / "steamapps" / "common" / inst):
            return lib / "steamapps" / "common" / inst
    for lib in libs:
        p = lib / "steamapps" / "common" / GAME_FOLDER
        if _looks_like_game(p):
            return p
    return None


def _mtime(p: Path) -> float:
    best = 0.0
    for q in (p, p / "tf3_planner", p / "save", p / "settings.lua"):
        try:
            best = max(best, q.stat().st_mtime)
        except OSError:
            pass
    return best


def userdata_candidates() -> list[Path]:
    """All <Steam>/userdata/<account>/3493540/local folders, most recently used first."""
    found: list[Path] = []
    for root in steam_roots():
        ud = root / "userdata"
        try:
            accounts = [a for a in ud.iterdir() if a.is_dir()]
        except OSError:
            continue
        for a in accounts:
            p = a / APP_ID / "local"
            if p.is_dir():
                found.append(p)
    found.sort(key=_mtime, reverse=True)
    return found


def discover_userdata_dir() -> Path | None:
    c = userdata_candidates()
    return c[0] if c else None


# ---------------------------------------------------------------------------- public API

def _override(env: str, key: str) -> Path | None:
    v = os.environ.get(env)
    if v:
        return Path(v).expanduser()
    v = _settings_raw().get(key)
    if isinstance(v, str) and v.strip():
        return Path(v.strip()).expanduser()
    return None


@functools.lru_cache(maxsize=None)
def userdata_dir() -> Path:
    """TF3 user folder (<Steam>/userdata/<account>/3493540/local). Never None: when nothing is found
    a placeholder under the first Steam folder is returned and `userdata_found()` is False."""
    p = _override(ENV_USERDATA, "userdata_dir") or discover_userdata_dir()
    if p is not None:
        return p
    roots = steam_roots()
    base = roots[0] if roots else Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Steam"
    return base / "userdata" / "<accountId>" / APP_ID / "local"


def userdata_found() -> bool:
    return userdata_dir().is_dir()


@functools.lru_cache(maxsize=None)
def game_dir() -> Path | None:
    """Transport Fever 3 install folder, or None if not found."""
    return _override(ENV_GAME, "game_dir") or discover_game_dir()


def require_game_dir() -> Path:
    g = game_dir()
    if g is None or not (g / "base").is_dir():
        raise FileNotFoundError(
            "Не найдена папка установки Transport Fever 3. Укажите её: переменная окружения "
            f"{ENV_GAME}, ключ \"game_dir\" в {settings_file()} или "
            "`python -m tf3planner.config --set-game \"<путь>\"`.")
    return g


def modio_mods_dir() -> Path:
    """Where the game's mod.io client keeps subscribed mods (C:\\Users\\Public\\mod.io\\10640\\mods)."""
    public = os.environ.get("PUBLIC") or r"C:\Users\Public"
    return Path(public) / "mod.io" / MODIO_GAME_ID / "mods"


def modio_installed(mod_id: str) -> list[Path]:
    """mod.io copies of our mod (folders whose mod.json has this modId)."""
    out = []
    try:
        dirs = [d for d in modio_mods_dir().iterdir() if d.is_dir()]
    except OSError:
        return out
    for d in dirs:
        try:
            if json.loads((d / "mod.json").read_text(encoding="utf-8")).get("modId") == mod_id:
                out.append(d)
        except (OSError, ValueError):
            pass
    return out


def report() -> dict:
    ud = userdata_dir()
    g = game_dir()
    return {
        "frozen": FROZEN,
        "code_root": str(CODE_ROOT),
        "app_dir": str(APP_DIR),
        "data_dir": str(data_dir()),
        "steam_roots": [str(p) for p in steam_roots()],
        "steam_libraries": [str(p) for p in steam_libraries()],
        "userdata_dir": str(ud),
        "userdata_found": ud.is_dir(),
        "userdata_candidates": [str(p) for p in userdata_candidates()],
        "game_dir": str(g) if g else None,
        "game_found": bool(g and (g / "base").is_dir()),
        "modio_mods_dir": str(modio_mods_dir()),
    }


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="TF3 Planner: show / override discovered paths")
    ap.add_argument("--set-userdata", metavar="PATH", help='folder <Steam>/userdata/<id>/3493540/local ("" = auto)')
    ap.add_argument("--set-game", metavar="PATH", help='Transport Fever 3 install folder ("" = auto)')
    args = ap.parse_args(argv)
    if args.set_userdata is not None:
        print("saved ->", save_override("userdata_dir", args.set_userdata))
    if args.set_game is not None:
        print("saved ->", save_override("game_dir", args.set_game))
    print(json.dumps(report(), ensure_ascii=False, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())

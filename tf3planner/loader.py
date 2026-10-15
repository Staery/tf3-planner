"""Load the TF3 Planner game export.

The mod writes via app.saveUserdata (io is disabled in TF3), producing
`<userdata>/tf3_planner/export.lua` of the form `function data() return {...} end`.
"""
from __future__ import annotations

import json
from pathlib import Path

from lupa.lua52 import LuaRuntime, lua_type

from .config import data_dir as _data_dir, userdata_dir as _userdata_dir

USERDATA = _userdata_dir()          # <Steam>/userdata/<account>/3493540/local (see tf3planner/config.py)
EXPORT_LUA = USERDATA / "tf3_planner" / "export.lua"


def _to_py(obj):
    if lua_type(obj) != "table":
        return obj
    keys = list(obj.keys())
    if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [_to_py(obj[k]) for k in range(1, len(keys) + 1)]
    return {str(k): _to_py(v) for k, v in obj.items()}


def load_export(path: Path = EXPORT_LUA) -> dict:
    lua = LuaRuntime(unpack_returned_tuples=True)
    lua.execute(path.read_text(encoding="utf-8"))
    return _to_py(lua.globals().data())


def export_to_json(src: Path = EXPORT_LUA, dst: Path | None = None) -> Path:
    data = load_export(src)
    dst = dst or _data_dir() / "export.json"
    dst.parent.mkdir(parents=True, exist_ok=True)
    dst.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    return dst


def summary(data: dict) -> str:
    """One line per export: version, errors and the size of the v5 calibration sections."""
    def n(x):
        return len(x) if isinstance(x, (list, dict)) else 0
    parts = [f"v{data.get('version')}", f"errors={n(data.get('errors'))}"]
    if data.get("version", 0) >= 5:
        parts += [f"lines={n(data.get('lines'))}",
                  f"stations={n((data.get('stations') or {}).get('stations'))}",
                  f"towns_stats={n((data.get('towns_stats') or {}).get('towns'))}",
                  f"company_level={(data.get('company') or {}).get('level')}"]
    errors = data.get("errors") or []
    for e in errors if isinstance(errors, list) else errors.values():
        parts.append(f"\n  error in {e.get('section')}: {e.get('error')}")
    return " ".join(parts)


if __name__ == "__main__":
    out = export_to_json()
    print(out)
    print(summary(json.loads(out.read_text(encoding="utf-8"))))

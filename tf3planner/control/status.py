"""Status cards for the control page (cheap: big JSON files are re-read only when their mtime changes)."""
from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime
from pathlib import Path

from . import deploy, settings as settings_mod
from .jobs import CONFLICTS, KINDS, MANAGER, module_exists
from .paths import (CALIBRATION_JSON, DOSSIERS_JSON, EXPORT_JSON, EXPORT_LUA, GAME_STDOUT, PKG, PLAN_JSON,
                    ROOT)

STARTED = time.time()
_cache: dict[str, tuple[float, dict]] = {}
_lock = threading.Lock()


def _mtime(p: Path) -> float | None:
    try:
        return p.stat().st_mtime
    except OSError:
        return None


def _cached(path: Path, fn) -> dict | None:
    mt = _mtime(path)
    if mt is None:
        return None
    with _lock:
        hit = _cache.get(str(path))
        if hit and hit[0] == mt:
            return hit[1]
    try:
        val = fn(json.loads(path.read_text(encoding="utf-8")))
    except Exception as e:
        val = {"error": f"{path.name}: {e}"}
    with _lock:
        _cache[str(path)] = (mt, val)
    return val


def _n(x) -> int:
    return len(x) if isinstance(x, (list, dict)) else 0


def _export_summary(d: dict) -> dict:
    meta = d.get("meta") or {}
    date = meta.get("date") or {}
    errors = d.get("errors") or {}
    err_list = list(errors.values()) if isinstance(errors, dict) else list(errors)
    company = d.get("company") or {}
    lines = d.get("lines")
    return {
        "version": d.get("version"),
        "date": date,
        "year": meta.get("year") or date.get("year"),
        "money": meta.get("money"),
        "loan": (meta.get("account") or {}).get("loan"),
        "towns": _n(d.get("towns")),
        "industries": _n(d.get("industries")),
        "lines": _n(lines) if lines is not None else None,
        "vehicles_models": _n(d.get("vehicles")),
        "company_level": company.get("level") if isinstance(company, dict) else None,
        "errors": [{"section": e.get("section"), "error": str(e.get("error"))[:300]} for e in err_list
                   if isinstance(e, dict)][:10],
        "errors_count": len(err_list),
    }


def _plan_summary(d: dict) -> dict:
    s = d.get("summary") or {}
    steps = d.get("steps") or []
    gen = d.get("generated")
    phases = {}
    for st in steps:
        if st.get("action") == "build_line":
            ph = st.get("phase", 1)
            phases[ph] = phases.get(ph, 0) + 1
    return {
        "generated": gen,
        "generated_ts": datetime.fromisoformat(gen).timestamp() if gen else None,
        "objective": d.get("objective"),
        "horizon": s.get("horizon"),
        "lines": s.get("lines"),
        "steps": len(steps),
        "start_money": s.get("start_money"),
        "final_money": s.get("final_money"),
        "total_capex": s.get("total_capex"),
        "loans": s.get("loans"),
        "phase_lines": phases,
        "note": s.get("note"),
    }


def _calibration_summary(d: dict) -> dict:
    fits = d.get("fits") or {}
    keys = ["income_factor", "running_factor", "trip_time_factor", "speed_factor", "load_factor"]
    out = {"version": d.get("version"), "lines": _n(d.get("lines")),
           "lines_used": fits.get("lines_used"), "lines_total": fits.get("lines_total"),
           "factors": {k: fits.get(k) for k in keys if fits.get(k) is not None}}
    if isinstance(fits.get("passengers"), dict):
        out["trips_per_resident"] = fits["passengers"].get("trips_per_resident")
    return out


def _dossier_summary(d: dict) -> dict:
    meta = d.get("_meta") or {}
    return {"lines": meta.get("lines", len([k for k in d if not k.startswith("_")])),
            "generated": meta.get("generated"), "plan_generated": meta.get("plan_generated")}


_ttl_cache: dict[str, tuple[float, object]] = {}


def _ttl(key: str, seconds: float, fn):
    hit = _ttl_cache.get(key)
    if hit and time.time() - hit[0] < seconds:
        return hit[1]
    val = fn()
    _ttl_cache[key] = (time.time(), val)
    return val


def code_mtime() -> float:
    """Newest .py of the web server + control center (to detect "server runs old code")."""
    newest = 0.0
    for sub in ("web", "control"):
        for p in (PKG / sub).glob("*.py"):
            newest = max(newest, _mtime(p) or 0)
    return newest


def collect(port: int | None = None) -> dict:
    s = settings_mod.load()
    lua_mt, json_mt = _mtime(EXPORT_LUA), _mtime(EXPORT_JSON)
    export = _cached(EXPORT_JSON, _export_summary) or {}
    plan = _cached(PLAN_JSON, _plan_summary)
    calib = _cached(CALIBRATION_JSON, _calibration_summary)
    doss = _cached(DOSSIERS_JSON, _dossier_summary)
    plan_ts = (plan or {}).get("generated_ts")

    planner_src = ""
    try:
        planner_src = (PKG / "planner.py").read_text(encoding="utf-8", errors="replace")
    except OSError:
        pass
    env_wired = {k: (k in planner_src) for k in settings_mod.planner_env(s)}

    from . import watcher
    return {
        "now": time.time(),
        "export": {
            "lua_path": str(EXPORT_LUA), "lua_time": lua_mt, "lua_size": EXPORT_LUA.stat().st_size if lua_mt else None,
            "json_time": json_mt,
            "pending": bool(lua_mt and (not json_mt or lua_mt > json_mt + 1)),
            **export,
        },
        "plan": None if plan is None else {
            **plan, "time": _mtime(PLAN_JSON),
            "stale": bool(json_mt and plan_ts and json_mt > plan_ts + 1),
        },
        "calibration": None if calib is None else {**calib, "time": _mtime(CALIBRATION_JSON)},
        "dossier": {"available": module_exists("tf3planner.dossier"),
                    **({} if doss is None else {**doss, "time": _mtime(DOSSIERS_JSON)})},
        "mod": _ttl("mod", 4, lambda: {**deploy.mod_state(), "game_running": deploy.game_running()}),
        "game_log": {"path": str(GAME_STDOUT), "time": _mtime(GAME_STDOUT)},
        "server": {"pid": os.getpid(), "port": port, "started": STARTED, "python": sys.version.split()[0],
                   "root": str(ROOT), "code_changed": code_mtime() > STARTED + 1},
        "settings": s,
        "env": settings_mod.planner_env(s),
        "env_wired": env_wired,
        "auto": watcher.WATCHER.state(),
        "jobs": MANAGER.list(),
        "kinds": [{"key": k.key, "title": k.title, "description": k.description, "group": k.group,
                   "available": k.available(), "conflicts": sorted(CONFLICTS.get(k.key, ()))}
                  for k in KINDS.values()],
        "last_event": MANAGER.last_event_id,
    }

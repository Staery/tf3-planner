"""HTTP API of the control center, mounted into the map server (tf3planner/web/server.py).

  GET  /control, /control.html          control page (static/control.html)
  GET  /api/control/ping                {"app": "tf3-planner", ...} - used by the launcher
  GET  /api/control/status              all status cards, settings, jobs, auto mode
  GET  /api/control/settings            data/settings.json (+ defaults)
  POST /api/control/settings            update settings (JSON body)
  GET  /api/control/jobs                running + last 20 jobs
  POST /api/control/run/{kind}          start a job (update, loader, planner, calibration, dossier, deploy, diag_*)
  POST /api/control/jobs/{id}/cancel    cancel (kills the process tree)
  GET  /api/control/jobs/{id}/log?since=N   log lines from index N (poll for live output)
  GET  /api/control/events?since=N      toast events (job finished, new export detected)
  GET  /api/control/gamelog             game stdout filtered to [TF3P] / Lua errors + hints
  POST /api/control/open/{target}       open a folder/file in Explorer (project, data, logs, export, mods, gamelog, mod_src)
  POST /api/control/shortcut            create the desktop shortcut "TF3 Planner"
  POST /api/control/shutdown            stop running jobs and the server
  POST /api/control/restart             restart the server (picks up new code)
"""
from __future__ import annotations

import os
import subprocess
import threading
import time

from fastapi import APIRouter, Body, FastAPI, Request
from fastapi.responses import FileResponse

from tf3planner.web.localized import JSONResponse

from . import gamelog, settings as settings_mod, status as status_mod
from .jobs import MANAGER, JobError
from .paths import (DATA, DETACHED, EXPORT_LUA, GAME_STDOUT, LOGS, MOD_DST, MOD_SRC, MODS_DIR, NEW_GROUP,
                    NO_WINDOW, ROOT, venv_python)
from .watcher import WATCHER

router = APIRouter()
_STATIC = ROOT / "tf3planner" / "web" / "static"
_state: dict = {"server": None, "port": None}

NO_CACHE = {"Cache-Control": "no-cache"}


def _port(request: Request | None = None) -> int | None:
    srv = _state["server"]
    if srv is not None:
        return srv.config.port
    if request is not None:
        return request.url.port
    return _state["port"]


def _err(msg: str, code: int = 409):
    return JSONResponse({"ok": False, "error": msg}, status_code=code)


# ---------------------------------------------------------------------------- page

@router.get("/control")
@router.get("/control.html")
def control_page():
    return FileResponse(_STATIC / "control.html", headers=NO_CACHE)


# ---------------------------------------------------------------------------- status / settings

@router.get("/api/control/ping")
def ping(request: Request):
    return {"app": "tf3-planner", "pid": os.getpid(), "started": status_mod.STARTED, "port": _port(request),
            "code_changed": status_mod.code_mtime() > status_mod.STARTED + 1,
            "jobs_running": len(MANAGER.running())}


@router.get("/api/control/status")
def status(request: Request):
    WATCHER.start()     # no-op when already running
    return JSONResponse(status_mod.collect(_port(request)), headers=NO_CACHE)


@router.get("/api/control/settings")
def get_settings():
    return {"settings": settings_mod.load(), "defaults": settings_mod.DEFAULTS,
            "env": settings_mod.planner_env(), "env_doc": settings_mod.ENV_DOC}


@router.post("/api/control/settings")
def post_settings(body: dict = Body(...)):
    before = settings_mod.load().get("lang")
    s = settings_mod.save(body)
    if s.get("lang") != before and (DATA / "plan.json").exists():
        # the in-game blueprint shows the plan texts in the chosen language: rewrite its file
        def rewrite():
            try:
                from tf3planner.overlay import write_overlay
                write_overlay()
            except Exception:  # noqa: BLE001 - the next plan update writes it anyway
                pass
        threading.Thread(target=rewrite, name="overlay-lang", daemon=True).start()
    return {"ok": True, "settings": s, "env": settings_mod.planner_env(s)}


# ---------------------------------------------------------------------------- jobs

@router.get("/api/control/jobs")
def jobs():
    return MANAGER.list()


@router.post("/api/control/run/{kind}")
def run(kind: str):
    try:
        job = MANAGER.start(kind)
    except JobError as e:
        return _err(str(e))
    return {"ok": True, "job": job.to_dict()}


@router.post("/api/control/jobs/{job_id}/cancel")
def cancel(job_id: str):
    try:
        job = MANAGER.cancel(job_id)
    except JobError as e:
        return _err(str(e), 404)
    return {"ok": True, "job": job.to_dict()}


@router.get("/api/control/jobs/{job_id}/log")
def job_log(job_id: str, since: int = 0):
    try:
        return JSONResponse(MANAGER.log(job_id, since), headers=NO_CACHE)
    except JobError as e:
        return _err(str(e), 404)


@router.get("/api/control/events")
def events(since: int = 0):
    return {"events": MANAGER.events_since(since), "last": MANAGER.last_event_id}


@router.get("/api/control/gamelog")
def game_log(limit: int = 200):
    return JSONResponse(gamelog.read(limit), headers=NO_CACHE)


# ---------------------------------------------------------------------------- desktop helpers

_TARGETS = {
    "project": ROOT, "data": DATA, "logs": LOGS, "export": EXPORT_LUA.parent, "mods": MODS_DIR,
    "mod_installed": MOD_DST, "mod_src": MOD_SRC, "gamelog": GAME_STDOUT,
}


@router.post("/api/control/open/{target}")
def open_target(target: str):
    path = _TARGETS.get(target)
    if path is None:
        return _err("неизвестная папка", 404)
    if not path.exists():
        if path.suffix:
            return _err(f"нет файла {path}", 404)
        path.mkdir(parents=True, exist_ok=True)
    try:
        if path.is_file():
            subprocess.Popen(["notepad.exe", str(path)], creationflags=0)
        else:
            os.startfile(str(path))     # Explorer
    except Exception as e:
        return _err(f"не удалось открыть: {e}", 500)
    return {"ok": True, "path": str(path)}


@router.post("/api/control/shortcut")
def shortcut():
    from . import shortcut as sc
    try:
        path = sc.create()
    except Exception as e:
        return _err(f"не удалось создать ярлык: {e}", 500)
    return {"ok": True, "path": str(path)}


def _exit_later(delay: float = 0.6):
    def go():
        time.sleep(delay)
        srv = _state["server"]
        if srv is not None:
            srv.should_exit = True
            time.sleep(5)
        os._exit(0)     # fallback (server started without main(), or stuck connections)
    threading.Thread(target=go, daemon=True).start()


@router.post("/api/control/shutdown")
def shutdown():
    MANAGER.cancel_all()
    WATCHER.stop()
    MANAGER.emit("Сервер выключается", "warn")
    _exit_later()
    return {"ok": True}


@router.post("/api/control/restart")
def restart(request: Request):
    if MANAGER.running():
        return _err("Идёт задача — дождитесь окончания или отмените её, затем перезапустите")
    port = _port(request) or 8010
    subprocess.Popen([venv_python(windowless=True), "-X", "utf8", "-m", "tf3planner.control.launcher",
                      "--restart", "--no-browser", "--port", str(port)],
                     cwd=str(ROOT), creationflags=DETACHED | NEW_GROUP | NO_WINDOW, close_fds=True,
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    WATCHER.stop()
    _exit_later(0.8)
    return {"ok": True}


# ---------------------------------------------------------------------------- wiring

def register(app: FastAPI):
    """Mount the control routes into the map app."""
    app.include_router(router)


def on_server_start(server=None, port: int | None = None):
    """Called from server.main(): remember the uvicorn.Server (for clean shutdown), start auto mode."""
    _state["server"] = server
    _state["port"] = port
    WATCHER.start()

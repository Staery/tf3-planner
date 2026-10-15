"""Auto mode: watch the game's export.lua; when a new export appears run the "update" pipeline
(loader -> calibration -> planner -> dossier) and post a toast event "План обновлён"."""
from __future__ import annotations

import threading
import time

from . import settings as settings_mod
from .jobs import CONFLICTS, MANAGER, JobError
from .paths import EXPORT_JSON, EXPORT_LUA

POLL_S = 2.0
STABLE_S = 3.0          # the file must stop changing for this long before we read it


def _stat():
    try:
        st = EXPORT_LUA.stat()
        return st.st_mtime, st.st_size
    except OSError:
        return None


class Watcher:
    def __init__(self):
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.handled_mtime: float | None = None      # export.lua mtime already turned into a plan / queued
        self.pending: float | None = None            # new export waiting (file still being written / job busy)
        self.last_change: float | None = None
        self.last_seen: tuple | None = None
        self.last_trigger: float | None = None
        self.last_job: str | None = None
        self.message = ""

    # ------------------------------------------------------------------ lifecycle
    def start(self):
        if self._thread and self._thread.is_alive():
            return
        st = _stat()
        try:
            json_mt = EXPORT_JSON.stat().st_mtime
        except OSError:
            json_mt = None
        # an export made while the server was off counts as new
        if st and json_mt and st[0] <= json_mt + 1:
            self.handled_mtime = st[0]
        self.last_seen = st
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="export-watcher", daemon=True)
        self._thread.start()
        MANAGER.on_finish.append(self._job_finished)

    def stop(self):
        self._stop.set()

    def state(self) -> dict:
        s = settings_mod.load()
        return {"enabled": bool(s.get("auto_mode")), "message": self.message, "pending": self.pending is not None,
                "last_trigger": self.last_trigger, "last_job": self.last_job,
                "watching": str(EXPORT_LUA), "alive": bool(self._thread and self._thread.is_alive())}

    # ------------------------------------------------------------------ loop
    def _loop(self):
        while not self._stop.wait(POLL_S):
            try:
                self._tick()
            except Exception as e:      # keep watching whatever happens
                self.message = f"ошибка наблюдателя: {e}"

    def _tick(self):
        st = _stat()
        if st != self.last_seen:
            self.last_seen = st
            self.last_change = time.time()
            if st and (self.handled_mtime is None or st[0] > self.handled_mtime + 0.5):
                if self.pending is None:
                    MANAGER.emit("Обнаружена новая выгрузка из игры", "info")
                self.pending = st[0]
        if not settings_mod.load().get("auto_mode"):
            self.message = "Авторежим выключен" + (" — есть новая выгрузка" if self.pending else "")
            return
        if self.pending is None:
            upd = next((j for j in MANAGER.running() if j.kind == "update"), None)
            self.message = (f"Обновляю план: {upd.steps[upd.step_index].label.lower()}…" if upd
                            else "Жду выгрузку из игры…")
            return
        if self.last_change and time.time() - self.last_change < STABLE_S:
            self.message = "Игра записывает выгрузку…"
            return
        busy = [j for j in MANAGER.running() if j.kind == "update" or j.kind in CONFLICTS.get("update", ())]
        if busy:
            self.message = f"Новая выгрузка — жду окончания «{busy[0].title}»"
            return
        try:
            job = MANAGER.start("update", trigger="auto")
        except JobError as e:
            self.message = str(e)
            return
        self.handled_mtime = self.pending
        self.pending = None
        self.last_trigger = time.time()
        self.last_job = job.id
        self.message = "Пересчитываю план по новой выгрузке…"
        MANAGER.emit("Новая выгрузка — запущен пересчёт плана", "info", job=job.id)

    # ------------------------------------------------------------------ notifications
    def _job_finished(self, job, failed_optional):
        titles = {"update": "План обновлён", "planner": "План пересчитан", "loader": "Данные из игры загружены",
                  "calibration": "Калибровка готова", "dossier": "Разбор маршрутов готов",
                  "deploy": "Мод установлен"}
        if job.status == "done":
            text = titles.get(job.kind, f"«{job.title}» — готово")
            if failed_optional:
                text += " (с ошибками: " + ", ".join(failed_optional) + ")"
            link = "/" if job.kind in ("update", "planner", "dossier") else None
            MANAGER.emit(text, "warn" if failed_optional else "good", link=link, job=job.id)
            if job.kind == "update" and job.started:
                # a manual full update consumed the newest export as well
                st = _stat()
                if st and st[0] <= job.started:
                    self.handled_mtime = max(self.handled_mtime or 0, st[0])
                    if self.pending and self.pending <= st[0]:
                        self.pending = None
        elif job.status == "failed":
            MANAGER.emit(f"«{job.title}» завершилась с ошибкой", "critical", job=job.id)
        elif job.status == "cancelled":
            MANAGER.emit(f"«{job.title}» отменена", "warn", job=job.id)
        if job.kind == "update" and job.status == "done":
            self.message = "План обновлён. Жду следующую выгрузку…"


WATCHER = Watcher()

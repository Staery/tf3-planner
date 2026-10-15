"""Background job runner for the control center.

A job is a sequence of steps; each step is a subprocess (venv python, `-X utf8 -u`, cwd = project
root) whose stdout+stderr are streamed line by line into the job log. Only one job of a kind runs
at a time, and kinds that touch the same files (CONFLICTS) exclude each other.

History of the last HISTORY_MAX finished jobs (metadata + log tail) is kept in data/jobs.json,
full logs in data/logs/<id>-<kind>.log.

Planner settings from data/settings.json are passed to every step as TF3P_* environment
variables (see settings.py).
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import gamestatus
from . import settings as settings_mod
from .paths import JOBS, LOGS, NO_WINDOW, PKG, ROOT, venv_python

HISTORY_MAX = 20
LOG_MAX_LINES = 50_000
TAIL_LINES = 80


def _py(module: str, *args: str) -> list[str]:
    return [venv_python(), "-X", "utf8", "-u", "-m", module, *args]


def module_exists(module: str) -> bool:
    rel = module.split(".", 1)[1].replace(".", "/") if module.startswith("tf3planner.") else None
    return bool(rel) and ((PKG / f"{rel}.py").exists() or (PKG / rel / "__main__.py").exists())


@dataclass
class Step:
    label: str
    argv: list[str]
    optional: bool = False          # failure does not stop the job
    env: dict = field(default_factory=dict)   # extra environment for this step only


@dataclass
class Kind:
    key: str
    title: str
    description: str
    steps: callable                 # (settings) -> list[Step]
    group: str = "main"             # UI grouping: main | diag
    module: str | None = None       # availability check

    def available(self) -> bool:
        return self.module is None or module_exists(self.module)


def _update_steps(s: dict) -> list[Step]:
    steps = [Step("Загрузка выгрузки из игры", _py("tf3planner.loader"))]
    if s.get("auto_calibrate") and module_exists("tf3planner.calibration"):
        steps.append(Step("Калибровка по линиям игры", _py("tf3planner.calibration"), optional=True))
    if s.get("quick_first", True):
        # fast plan for the next ~10 game years first: the in-game blueprint refreshes within minutes
        steps.append(Step(f"Быстрый план (+{int(s.get('quick_years', 10))} лет)", _py("tf3planner.planner"),
                          optional=True, env={"TF3P_HORIZON": "0",
                                              "TF3P_HORIZON_REL": str(int(s.get("quick_years", 10)))}))
    steps.append(Step("Расчёт плана", _py("tf3planner.planner")))
    if s.get("auto_dossier") and module_exists("tf3planner.dossier"):
        steps.append(Step("Подробный разбор маршрутов", _py("tf3planner.dossier"), optional=True))
    return steps


KINDS: dict[str, Kind] = {k.key: k for k in [
    Kind("update", "Полное обновление", "Выгрузка → калибровка → план → разбор маршрутов", _update_steps),
    Kind("loader", "Обновить данные из игры", "export.lua → data/export.json",
         lambda s: [Step("Загрузка выгрузки из игры", _py("tf3planner.loader"))], module="tf3planner.loader"),
    Kind("planner", "Пересчитать план", "data/plan.json + data/routes.json (несколько минут)",
         lambda s: [Step("Расчёт плана", _py("tf3planner.planner"))], module="tf3planner.planner"),
    Kind("calibration", "Калибровка", "Сравнение линий в игре с моделью → data/calibration.json",
         lambda s: [Step("Калибровка", _py("tf3planner.calibration"))], module="tf3planner.calibration"),
    Kind("dossier", "Подробный разбор маршрутов", "Инструкции по каждой линии → data/dossiers.json",
         lambda s: [Step("Разбор маршрутов", _py("tf3planner.dossier"))], module="tf3planner.dossier"),
    Kind("deploy", "Установить мод в игру", "Копирование мода в папку игры + _content.json",
         lambda s: [Step("Установка мода", _py("tf3planner.control.deploy"))], module="tf3planner.control.deploy"),
    Kind("diag_flow", "Потоки грузов (MILP)", "Оптимальная сеть потоков: какие пары и сколько везти",
         lambda s: [Step("flow", _py("tf3planner.flow"))], group="diag", module="tf3planner.flow"),
    Kind("diag_passengers", "Пассажиры", "Отчёт по пассажирским линиям внутри и между городами",
         lambda s: [Step("passengers", _py("tf3planner.passengers"))], group="diag", module="tf3planner.passengers"),
    Kind("diag_multimodal", "Перевалка через склады", "Цепочки с пересадкой грузов",
         lambda s: [Step("multimodal", _py("tf3planner.multimodal"))], group="diag", module="tf3planner.multimodal"),
    Kind("diag_capacity", "Пропускная способность", "Таблицы окупаемости ж/д vs авто по эпохам",
         lambda s: [Step("capacity", _py("tf3planner.capacity"))], group="diag", module="tf3planner.capacity"),
]}

_CONFLICT_PAIRS = [("update", k) for k in ("loader", "planner", "calibration", "dossier", "diag_flow",
                                            "diag_passengers", "diag_multimodal", "diag_capacity")] + [
    ("loader", "planner"), ("loader", "calibration"), ("loader", "dossier"), ("planner", "dossier")]
CONFLICTS: dict[str, set[str]] = {}
for a, b in _CONFLICT_PAIRS:
    CONFLICTS.setdefault(a, set()).add(b)
    CONFLICTS.setdefault(b, set()).add(a)

_PROGRESS_RE = re.compile(r"progress (\d{4})/(\d{4})")
_RANGE_RE = re.compile(r"planner: (\d{4})\.\.(\d{4})")


class JobError(Exception):
    pass


@dataclass
class Job:
    id: str
    kind: str
    title: str
    steps: list[Step]
    trigger: str = "manual"
    status: str = "queued"          # queued | running | done | failed | cancelled
    created: float = field(default_factory=time.time)
    started: float | None = None
    finished: float | None = None
    exit_code: int | None = None
    step_index: int = 0
    step_results: list[dict] = field(default_factory=list)
    log: list[str] = field(default_factory=list)
    log_base: int = 0               # absolute index of log[0] (log is trimmed from the front)
    progress: float | None = None
    progress_text: str = ""
    log_file: str | None = None
    proc: subprocess.Popen | None = None
    cancel_requested: bool = False
    _range: tuple[int, int] | None = None

    @property
    def log_end(self) -> int:
        return self.log_base + len(self.log)

    @property
    def active(self) -> bool:
        return self.status in ("queued", "running")

    def duration(self) -> float | None:
        if not self.started:
            return None
        return (self.finished or time.time()) - self.started

    def to_dict(self, tail: int = 0) -> dict:
        d = {
            "id": self.id, "kind": self.kind, "title": self.title, "trigger": self.trigger,
            "status": self.status, "created": self.created, "started": self.started, "finished": self.finished,
            "duration": self.duration(), "exit_code": self.exit_code,
            "step_index": self.step_index, "steps": [s.label for s in self.steps],
            "step_results": self.step_results, "progress": self.progress, "progress_text": self.progress_text,
            "log_end": self.log_end, "log_file": self.log_file,
        }
        if tail:
            d["log_tail"] = self.log[-tail:]
        return d


class JobManager:
    def __init__(self):
        self._lock = threading.RLock()
        self.jobs: dict[str, Job] = {}
        self.history: list[dict] = self._load_history()
        # a previous server process may have died mid-job: don't let the in-game window show "running"
        try:
            st = gamestatus.STATUS_FILE.read_text(encoding="utf-8") if gamestatus.STATUS_FILE.exists() else ""
            if 'state = "running"' in st:
                gamestatus.write(force=True, state="cancelled", progress=-1.0,
                                 text="Пересчёт прерван (перезапуск центра управления)")
        except OSError:
            pass
        self.events: list[dict] = []
        self._event_seq = 0
        self.on_finish: list = []       # callbacks(job)

    # ------------------------------------------------------------------ history / events
    def _load_history(self) -> list[dict]:
        try:
            h = json.loads(JOBS.read_text(encoding="utf-8"))
            return h if isinstance(h, list) else []
        except (OSError, ValueError):
            return []

    def _save_history(self):
        JOBS.parent.mkdir(parents=True, exist_ok=True)
        JOBS.write_text(json.dumps(self.history[-HISTORY_MAX:], ensure_ascii=False, indent=1), encoding="utf-8")
        keep = {Path(h["log_file"]).name for h in self.history[-HISTORY_MAX:] if h.get("log_file")}
        keep |= {Path(j.log_file).name for j in self.jobs.values() if j.log_file}
        try:
            for f in LOGS.glob("*.log"):
                if f.name not in keep:
                    f.unlink()
        except OSError:
            pass

    def emit(self, text: str, level: str = "info", link: str | None = None, job: str | None = None):
        with self._lock:
            self._event_seq += 1
            self.events.append({"id": self._event_seq, "ts": time.time(), "level": level, "text": text,
                                "link": link, "job": job})
            self.events = self.events[-100:]

    def events_since(self, since: int) -> list[dict]:
        with self._lock:
            return [e for e in self.events if e["id"] > since]

    @property
    def last_event_id(self) -> int:
        return self._event_seq

    # ------------------------------------------------------------------ queries
    def running(self) -> list[Job]:
        with self._lock:
            return [j for j in self.jobs.values() if j.active]

    def running_kind(self, kind: str) -> Job | None:
        return next((j for j in self.running() if j.kind == kind), None)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return self.jobs.get(job_id)

    def list(self) -> dict:
        with self._lock:
            live = [j.to_dict(tail=3) for j in sorted(self.jobs.values(), key=lambda j: j.created)]
            live_ids = {j["id"] for j in live}
            hist = [h for h in self.history if h["id"] not in live_ids]
            return {"running": [j for j in live if j["status"] in ("queued", "running")],
                    "recent": (hist + [j for j in live if j["status"] not in ("queued", "running")])[-HISTORY_MAX:][::-1]}

    def log(self, job_id: str, since: int = 0) -> dict:
        j = self.get(job_id)
        if j is None:
            h = next((h for h in self.history if h["id"] == job_id), None)
            if h is None:
                raise JobError("нет такой задачи")
            lines = []
            if h.get("log_file") and Path(h["log_file"]).exists():
                lines = Path(h["log_file"]).read_text(encoding="utf-8", errors="replace").splitlines()
            else:
                lines = h.get("log_tail") or []
            return {"job": h, "lines": lines[since:], "next": len(lines), "base": 0}
        with self._lock:
            start = max(since, j.log_base)
            return {"job": j.to_dict(), "lines": j.log[start - j.log_base:], "next": j.log_end,
                    "base": j.log_base, "skipped": start > since}

    # ------------------------------------------------------------------ control
    def start(self, kind: str, trigger: str = "manual") -> Job:
        k = KINDS.get(kind)
        if k is None:
            raise JobError(f"неизвестная задача {kind}")
        if not k.available():
            raise JobError(f"«{k.title}»: модуль {k.module} ещё не установлен")
        with self._lock:
            busy = self.running_kind(kind)
            if busy:
                raise JobError(f"«{k.title}» уже выполняется")
            clash = [j for j in self.running() if j.kind in CONFLICTS.get(kind, ())]
            if clash:
                raise JobError(f"Сначала дождитесь окончания «{clash[0].title}» (или отмените её)")
            s = settings_mod.load()
            steps = k.steps(s)
            jid = time.strftime("%Y%m%d-%H%M%S") + f"-{kind}"
            while jid in self.jobs:
                jid += "x"
            job = Job(id=jid, kind=kind, title=k.title, steps=steps, trigger=trigger)
            LOGS.mkdir(parents=True, exist_ok=True)
            job.log_file = str(LOGS / f"{jid}.log")
            self.jobs[jid] = job
            # forget finished in-memory jobs beyond the history size
            done = [j for j in self.jobs.values() if not j.active]
            for old in sorted(done, key=lambda j: j.created)[:-HISTORY_MAX]:
                self.jobs.pop(old.id, None)
        threading.Thread(target=self._run, args=(job, s), name=f"job-{jid}", daemon=True).start()
        return job

    def cancel(self, job_id: str) -> Job:
        j = self.get(job_id)
        if j is None:
            raise JobError("нет такой задачи")
        if not j.active:
            return j
        j.cancel_requested = True
        self._kill(j)
        return j

    def cancel_all(self):
        for j in self.running():
            j.cancel_requested = True
            self._kill(j)

    @staticmethod
    def _kill(j: Job):
        p = j.proc
        if p is None or p.poll() is not None:
            return
        try:
            if os.name == "nt":
                subprocess.run(["taskkill", "/PID", str(p.pid), "/T", "/F"], capture_output=True, timeout=10,
                               creationflags=NO_WINDOW)
            else:
                p.kill()
        except Exception:
            try:
                p.kill()
            except Exception:
                pass

    # ------------------------------------------------------------------ worker
    def _append(self, job: Job, line: str, fh):
        line = line.rstrip("\n")
        with self._lock:
            job.log.append(line)
            if len(job.log) > LOG_MAX_LINES:
                cut = len(job.log) - LOG_MAX_LINES
                del job.log[:cut]
                job.log_base += cut
            m = _RANGE_RE.search(line)
            if m:
                job._range = (int(m.group(1)), int(m.group(2)))
            m = _PROGRESS_RE.search(line)
            if m:
                y, end = int(m.group(1)), int(m.group(2))
                start = job._range[0] if job._range else None
                if start is None:
                    job._range = (y - 1, end)
                    start = y - 1
                job.progress = max(0.0, min(1.0, (y - start + 1) / max(1, end - start + 1)))
                job.progress_text = line.strip()
                gamestatus.write(state="running", kind=job.kind, step=job.step_index + 1, steps=len(job.steps),
                                 label=job.steps[job.step_index].label if job.steps else "",
                                 progress=job.progress, year=y, end=end, text=line.strip())
        if fh:
            try:
                fh.write(line + "\n")
                fh.flush()
            except Exception:
                pass

    def _run(self, job: Job, s: dict):
        env = dict(os.environ)
        env.update(settings_mod.planner_env(s))
        env.update({"PYTHONIOENCODING": "utf-8", "PYTHONUNBUFFERED": "1", "PYTHONUTF8": "1"})
        fh = None
        try:
            fh = open(job.log_file, "w", encoding="utf-8")
        except OSError:
            pass
        job.status = "running"
        job.started = time.time()
        rc = 0
        failed_optional = []
        try:
            for i, step in enumerate(job.steps):
                if job.cancel_requested:
                    break
                job.step_index = i
                job.progress = None
                job._range = None
                t0 = time.time()
                prefix = f"[{i + 1}/{len(job.steps)}] " if len(job.steps) > 1 else ""
                self._append(job, f"▶ {prefix}{step.label}", fh)
                gamestatus.write(force=True, state="running", kind=job.kind, step=i + 1, steps=len(job.steps),
                                 label=step.label, progress=-1.0, text=step.label)
                try:
                    job.proc = subprocess.Popen(step.argv, cwd=str(ROOT), env={**env, **step.env},
                                                stdout=subprocess.PIPE,
                                                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                                                creationflags=NO_WINDOW)
                except OSError as e:
                    self._append(job, f"не удалось запустить: {e}", fh)
                    rc = -1
                else:
                    buf = b""
                    while True:
                        chunk = job.proc.stdout.read1(65536) if hasattr(job.proc.stdout, "read1") \
                            else job.proc.stdout.readline()
                        if not chunk:
                            break
                        buf += chunk
                        hold = b""
                        if buf.endswith(b"\r"):         # may be the first half of \r\n
                            buf, hold = buf[:-1], b"\r"
                        *parts, buf = re.split(rb"\r\n|\n|\r", buf)
                        buf += hold
                        for part in parts:
                            self._append(job, part.decode("utf-8", "replace"), fh)
                    if buf:
                        self._append(job, buf.decode("utf-8", "replace"), fh)
                    rc = job.proc.wait()
                dt = time.time() - t0
                job.step_results.append({"label": step.label, "exit_code": rc, "duration": round(dt, 1),
                                         "optional": step.optional})
                if job.cancel_requested:
                    break
                if rc != 0:
                    if step.optional:
                        self._append(job, f"✖ шаг «{step.label}» завершился с кодом {rc} — продолжаю", fh)
                        failed_optional.append(step.label)
                        rc = 0
                        continue
                    self._append(job, f"✖ шаг «{step.label}» завершился с кодом {rc}", fh)
                    break
                self._append(job, f"✔ {step.label}: {dt:.1f} с", fh)
        except Exception as e:      # never leave a job hanging in "running"
            self._append(job, f"внутренняя ошибка: {e!r}", fh)
            rc = -1
        finally:
            job.proc = None
            job.finished = time.time()
            job.exit_code = rc
            job.status = "cancelled" if job.cancel_requested else ("done" if rc == 0 else "failed")
            if job.status == "done":
                job.progress = 1.0
            gamestatus.write(force=True, state=job.status, kind=job.kind, step=len(job.steps), steps=len(job.steps),
                             label="", progress=job.progress if job.progress is not None else -1.0,
                             text={"done": "План обновлён", "failed": f"Ошибка (код {rc})",
                                   "cancelled": "Отменено"}.get(job.status, job.status))
            self._append(job, {"done": f"Готово за {job.duration():.0f} с",
                               "failed": f"Ошибка (код {rc})",
                               "cancelled": "Отменено"}[job.status], fh)
            if fh:
                fh.close()
            with self._lock:
                rec = job.to_dict(tail=TAIL_LINES)
                rec["failed_optional"] = failed_optional
                self.history = [h for h in self.history if h["id"] != job.id] + [rec]
                self.history = self.history[-HISTORY_MAX:]
                self._save_history()
            for cb in list(self.on_finish):
                try:
                    cb(job, failed_optional)
                except Exception:
                    pass


MANAGER = JobManager()

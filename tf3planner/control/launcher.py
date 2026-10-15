"""One-click launcher: start the TF3 Planner server in the background (no console) and open the
control page in the browser. If the server already runs, just open the browser.

  pythonw -m tf3planner.control.launcher [--port 8010] [--no-browser] [--restart]

Used by "TF3 Planner.bat" / "TF3 Planner.vbs" and the desktop shortcut.
"""
from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.request
import webbrowser

from .paths import DEFAULT_PORT, DETACHED, NEW_GROUP, NO_WINDOW, ROOT, SERVER_LOG, venv_python

HOST = "127.0.0.1"
BREAKAWAY = 0x01000000      # CREATE_BREAKAWAY_FROM_JOB


def message(text: str, error: bool = True):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(None, text, "TF3 Planner", 0x10 if error else 0x40)
    except Exception:
        print(text, file=sys.stderr)


def _get(url: str, timeout: float = 1.5):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def ping(port: int) -> dict | None:
    try:
        code, body = _get(f"http://{HOST}:{port}/api/control/ping")
        d = json.loads(body)
        return d if code == 200 and d.get("app") == "tf3-planner" else None
    except Exception:
        return None


def port_busy(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex((HOST, port)) == 0


def listener_pid(port: int) -> int | None:
    try:
        out = subprocess.run(["netstat", "-ano", "-p", "TCP"], capture_output=True, timeout=10,
                             creationflags=NO_WINDOW).stdout.decode("ascii", "ignore")
    except Exception:
        return None
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 5 and parts[1].endswith(f":{port}") and parts[3].upper() == "LISTENING":
            try:
                return int(parts[4])
            except ValueError:
                pass
    return None


def process_cmdline(pid: int) -> str:
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command",
                              f"(Get-CimInstance Win32_Process -Filter 'ProcessId={pid}').CommandLine"],
                             capture_output=True, timeout=15, creationflags=NO_WINDOW).stdout
        return out.decode("utf-8", "ignore") + out.decode("cp866", "ignore")
    except Exception:
        return ""


def stop_old_server(port: int) -> bool:
    """An older planner server (without the control API) holds the port: stop it."""
    pid = listener_pid(port)
    if not pid:
        return False
    if "tf3planner.web.server" not in process_cmdline(pid):
        return False
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], capture_output=True, timeout=10,
                   creationflags=NO_WINDOW)
    return wait(lambda: not port_busy(port), 15)


def wait(cond, seconds: float, step: float = 0.3) -> bool:
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(step)
    return cond()


def start_server(port: int):
    try:
        if SERVER_LOG.exists() and SERVER_LOG.stat().st_size > 5_000_000:
            SERVER_LOG.unlink()
    except OSError:
        pass
    SERVER_LOG.parent.mkdir(parents=True, exist_ok=True)
    log = open(SERVER_LOG, "a", encoding="utf-8")
    log.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} launcher: starting server on port {port}\n")
    log.flush()
    argv = [venv_python(), "-X", "utf8", "-u", "-m", "tf3planner.web.server", "--port", str(port)]
    kw = dict(cwd=str(ROOT), stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT, close_fds=True)
    flags = DETACHED | NEW_GROUP | NO_WINDOW
    try:
        return subprocess.Popen(argv, creationflags=flags | BREAKAWAY, **kw)
    except OSError:
        return subprocess.Popen(argv, creationflags=flags, **kw)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="TF3 Planner launcher")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--restart", action="store_true", help="wait for the running server to exit, then start")
    args = ap.parse_args(argv)
    port = args.port
    url = f"http://{HOST}:{port}/control.html"

    info = ping(port)
    if args.restart and info:
        wait(lambda: ping(port) is None and not port_busy(port), 20)
        info = ping(port)
    elif info and info.get("code_changed") and not info.get("jobs_running"):
        # the program was updated since the server started: restart it to pick up the new code
        try:
            req = urllib.request.Request(f"http://{HOST}:{port}/api/control/restart", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=3).read()
            old = info["pid"]
            wait(lambda: (ping(port) or {}).get("pid") not in (None, old), 60, 0.5)
        except Exception:
            pass
        info = ping(port)

    if not info:
        if port_busy(port) and not stop_old_server(port):
            if not wait(lambda: not port_busy(port), 3):
                message(f"Порт {port} занят другой программой — TF3 Planner не может запуститься.\n"
                        f"Закройте её или перезагрузите компьютер.")
                return 1
        proc = start_server(port)
        ok = wait(lambda: ping(port) is not None or proc.poll() is not None, 90, 0.4)
        if not ok or ping(port) is None:
            tail = ""
            try:
                tail = "\n".join(SERVER_LOG.read_text(encoding="utf-8", errors="replace").splitlines()[-12:])
            except OSError:
                pass
            message("Не удалось запустить сервер TF3 Planner.\n\n" + tail +
                    f"\n\nПолный журнал: {SERVER_LOG}")
            return 1
    if not args.no_browser:
        webbrowser.open(url)
    return 0


if __name__ == "__main__":
    sys.exit(main())

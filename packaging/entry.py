"""Entry point of the packaged TF3 Planner (PyInstaller one-folder build).

Two executables are built from this script and share one _internal folder:

  TF3Planner.exe       windowed: double-click -> start the server in the background, open the browser
  tf3planner-cli.exe   console:  used by the control center for jobs, and by power users

The control center starts its helpers with python-style command lines
(`<python> -X utf8 -u -m tf3planner.planner ...`, see tf3planner/control/paths.venv_python), so both
executables accept that syntax:

  tf3planner-cli.exe -m tf3planner.config                  show discovered paths
  tf3planner-cli.exe -m tf3planner.firstrun --force        re-extract recipes after a game update
  tf3planner-cli.exe -m tf3planner.control.deploy          install the bundled mod into the game
  TF3Planner.exe [--port 8010] [--no-browser] [--restart]  (= tf3planner.control.launcher)

Path overrides for this run and every child process (they set TF3P_USERDATA / TF3P_GAME / TF3P_DATA):
  --userdata <Steam>/userdata/<id>/3493540/local   --game <TF3 install>   --data <folder for plans>
"""
from __future__ import annotations

import multiprocessing
import os
import sys

_PY_FLAGS_NOARG = {"-u", "-B", "-E", "-s", "-S", "-I", "-O", "-OO", "-q"}
_PATH_OPTS = {"--userdata": "TF3P_USERDATA", "--game": "TF3P_GAME", "--data": "TF3P_DATA"}


def _split(argv: list[str]):
    """-> (mode, target, rest, unbuffered); mode: 'module' | 'code' | 'app'."""
    unbuffered = False
    i = 0
    while i < len(argv):
        a = argv[i]
        if a == "-X" or a == "-W":
            i += 2
        elif a.startswith("-X") or a.startswith("-W"):
            i += 1
        elif a in _PY_FLAGS_NOARG:
            unbuffered |= a == "-u"
            i += 1
        elif a == "-m" and i + 1 < len(argv):
            return "module", argv[i + 1], argv[i + 2:], unbuffered
        elif a == "-c" and i + 1 < len(argv):
            return "code", argv[i + 1], argv[i + 2:], unbuffered
        else:
            break
    return "app", None, argv[i:], unbuffered


def _path_overrides(rest: list[str]) -> list[str]:
    out, i = [], 0
    while i < len(rest):
        a = rest[i]
        key, _, val = a.partition("=")
        if key in _PATH_OPTS:
            if not val and i + 1 < len(rest):
                val = rest[i + 1]
                i += 1
            os.environ[_PATH_OPTS[key]] = val
        else:
            out.append(a)
        i += 1
    return out


def _streams(unbuffered: bool):
    # windowed exe: no console -> stdout/stderr are None; send them to a log so print() never fails
    if sys.stdout is None or sys.stderr is None:
        try:
            from tf3planner import config
            log_dir = config.data_dir() / "logs"
            log_dir.mkdir(parents=True, exist_ok=True)
            f = open(log_dir / "app.log", "a", encoding="utf-8", buffering=1)
        except OSError:
            f = open(os.devnull, "w", encoding="utf-8")
        sys.stdout = sys.stdout or f
        sys.stderr = sys.stderr or f
    for s in (sys.stdout, sys.stderr):
        try:
            s.reconfigure(encoding="utf-8", errors="replace", write_through=unbuffered or None)
        except (AttributeError, ValueError, TypeError):
            try:
                s.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                pass


def _firstrun():
    from tf3planner import firstrun
    msgs: list[str] = []
    try:
        res = firstrun.ensure(log=msgs.append)
    except Exception as e:  # noqa: BLE001
        msgs.append(f"{type(e).__name__}: {e}")
        res = {}
    for m in msgs:
        print("[firstrun]", m)
    if not res.get("recipes"):
        try:
            import ctypes
            ctypes.windll.user32.MessageBoxW(
                None,
                "Не удалось подготовить рецепты производств из файлов игры:\n\n" + "\n".join(msgs) +
                "\n\nПланировщик запустится, но расчёт плана будет недоступен, пока папка "
                "Transport Fever 3 не найдена.\nУкажите её: TF3Planner.exe --game \"<папка игры>\" "
                "или tf3planner-cli.exe -m tf3planner.config --set-game \"<папка игры>\".",
                "TF3 Planner", 0x30)
        except Exception:  # noqa: BLE001
            pass


def main() -> int:
    multiprocessing.freeze_support()            # worker processes of a parallel planner
    mode, target, rest, unbuffered = _split(sys.argv[1:])
    rest = _path_overrides(rest)
    _streams(unbuffered)

    if mode == "module":
        import runpy
        if target in ("tf3planner.web.server",):
            from tf3planner import firstrun
            firstrun.ensure(log=lambda m: print("[firstrun]", m))
        sys.argv = [target, *rest]
        runpy.run_module(target, run_name="__main__", alter_sys=True)
        return 0
    if mode == "code":
        sys.argv = ["-c", *rest]
        exec(compile(target, "<string>", "exec"), {"__name__": "__main__"})
        return 0

    if rest[:1] in (["--version"], ["-V"]):
        from tf3planner import config
        print(f"TF3 Planner {config.APP_VERSION}")
        return 0
    if rest[:1] in (["--paths"],):
        from tf3planner import config
        return config.main([])
    _firstrun()
    from tf3planner.control import launcher
    return launcher.main(rest)


if __name__ == "__main__":
    sys.exit(main())

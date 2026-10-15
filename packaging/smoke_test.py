"""Smoke test of the built app: python packaging/smoke_test.py [dist/TF3Planner]

Uses a throw-away data folder (TF3P_DATA) and a free port, so the developer's data/ and a running
planner on 8010 are not touched. Checks: version, path discovery, bundled libraries (scipy HiGHS,
lupa, ortools, a process pool), first-run recipe extraction, the launcher starting the server,
/control.html + API answering, and a clean shutdown.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

NO_WINDOW = 0x08000000


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(url: str, timeout: float = 5.0):
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.status, r.read()


def run(argv, env, timeout=180) -> str:
    r = subprocess.run(argv, env=env, capture_output=True, timeout=timeout, creationflags=NO_WINDOW)
    out = r.stdout.decode("utf-8", "replace") + r.stderr.decode("utf-8", "replace")
    if r.returncode != 0:
        raise SystemExit(f"FAILED ({r.returncode}): {' '.join(map(str, argv))}\n{out}")
    return out


def main() -> int:
    dist = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent / "dist" / "TF3Planner")
    gui, cli = dist / "TF3Planner.exe", dist / "tf3planner-cli.exe"
    assert gui.exists() and cli.exists(), f"no executables in {dist}"
    tmp = Path(tempfile.mkdtemp(prefix="tf3p_smoke_"))
    env = dict(os.environ, TF3P_DATA=str(tmp))
    ok = []

    out = run([cli, "--version"], env)
    ok.append(out.strip())
    rep = json.loads(run([cli, "-m", "tf3planner.config"], env))
    ok.append(f"paths: data={rep['data_dir']} userdata_found={rep['userdata_found']} game_found={rep['game_found']}")
    assert rep["data_dir"] == str(tmp), rep["data_dir"]

    code = ("import scipy.optimize as o, numpy as np, lupa.lua52 as l, tf3planner.tours as t, math;"
            "from concurrent.futures import ProcessPoolExecutor as P;"
            "r = o.linprog([1, 2], A_ub=[[-1, -1]], b_ub=[-1], bounds=[(0, None)] * 2, method='highs');"
            "pool = list(P(2).map(math.sqrt, [4.0, 9.0]));"
            "print('highs', r.status, round(r.fun, 3), 'lua', l.LuaRuntime().eval('1+1'),"
            " 'ortools', t.HAVE_ORTOOLS, 'elkai', t.HAVE_ELKAI, 'pool', pool)")
    ok.append(run([cli, "-X", "utf8", "-u", "-c", code], env).strip())

    port = free_port()
    t0 = time.time()
    r = subprocess.run([gui, "--no-browser", "--port", str(port)], env=env, timeout=240)
    ok.append(f"launcher exit={r.returncode} in {time.time() - t0:.1f}s")
    try:
        assert r.returncode == 0, "launcher failed: see " + str(tmp / "web_server.log")
        st, body = get(f"http://127.0.0.1:{port}/control.html")
        assert st == 200 and b"<html" in body.lower(), "control.html"
        ok.append(f"/control.html 200, {len(body)} bytes")
        st, body = get(f"http://127.0.0.1:{port}/api/control/ping")
        ping = json.loads(body)
        assert ping.get("app") == "tf3-planner", ping
        ok.append(f"/api/control/ping ok pid={ping.get('pid')}")
        st, body = get(f"http://127.0.0.1:{port}/api/control/status", timeout=30)
        ok.append(f"/api/control/status {st}, {len(body)} bytes")
        st, body = get(f"http://127.0.0.1:{port}/")
        ok.append(f"/ (map) {st}")
        rec = tmp / "industry_recipes.json"
        ok.append(f"first run: industry_recipes.json {'present' if rec.exists() else 'MISSING'}")
    finally:
        try:
            req = urllib.request.Request(f"http://127.0.0.1:{port}/api/control/shutdown", data=b"", method="POST")
            urllib.request.urlopen(req, timeout=5).read()
        except Exception as e:  # noqa: BLE001
            ok.append(f"shutdown request: {e}")
        end = time.time() + 20
        while time.time() < end:
            with socket.socket() as s:
                s.settimeout(0.5)
                if s.connect_ex(("127.0.0.1", port)) != 0:
                    break
            time.sleep(0.3)
        else:
            ok.append("WARNING: server still listening after shutdown")
        ok.append("server stopped")
    print("\n".join(ok))
    print(f"data folder of the test: {tmp}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

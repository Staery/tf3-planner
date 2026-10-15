"""Job status for the in-game mod window ("Чертёж"): written as a Lua userdata file the mod can
read with app.loadUserdata("tf3_planner", "status") -> <userdata>/tf3_planner/status.lua.

Fields: state (idle|running|done|failed|cancelled), kind, step, steps, label, progress (0..1 or -1),
text, year/end (planner progress), plan_generated, updated (unix time), overlay_updated.
The mod shows "Пересчёт плана: 1925 / 2030, 40 %" and reloads the blueprint when state becomes
"done" and overlay_updated changed.
"""
from __future__ import annotations

import time
from pathlib import Path

from .paths import USERDATA

STATUS_FILE = USERDATA / "tf3_planner" / "status.lua"
_last_write = 0.0
MIN_INTERVAL_S = 2.0


def _lua(v) -> str:
    if v is None:
        return "nil"
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return f"{float(v):.3f}" if isinstance(v, float) else str(int(v))
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", " ").replace("\r", " ")
    return f'"{s}"'


def write(force: bool = False, **fields) -> None:
    """Write the status file (throttled unless force). Never raises."""
    global _last_write
    now = time.time()
    if not force and now - _last_write < MIN_INTERVAL_S:
        return
    _last_write = now
    fields.setdefault("updated", int(now))
    try:                        # the in-game window shows these texts: same language as the plan
        from . import settings
        if settings.load().get("lang") == "en":
            from tf3planner import i18n
            fields = {k: i18n.translate(v, "en") if isinstance(v, str) else v for k, v in fields.items()}
    except Exception:  # noqa: BLE001 - status must never fail
        pass
    overlay = USERDATA / "tf3_planner" / "plan_overlay.lua"
    try:
        fields.setdefault("overlay_updated", int(overlay.stat().st_mtime) if overlay.exists() else 0)
    except OSError:
        pass
    body = ",\n".join(f"\t\t{k} = {_lua(v)}" for k, v in fields.items())
    text = "function data()\n\treturn {\n" + body + "\n\t}\nend\n"
    try:
        STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
        tmp = STATUS_FILE.with_suffix(".tmp")
        tmp.write_text(text, encoding="utf-8")
        tmp.replace(STATUS_FILE)
    except OSError:
        pass

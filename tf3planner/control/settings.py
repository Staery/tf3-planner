"""Control-center settings, persisted to data/settings.json.

Planner settings are passed to `python -m tf3planner.planner` as environment variables
(read in planner.main()):

  TF3P_HORIZON     last simulated year, e.g. "2030" ("0"/empty = planner default)
  TF3P_PASSENGERS  "1"/"0"  passenger-line generator (passengers.py)
  TF3P_MULTIMODAL  "1"/"0"  warehouse/transfer chain generator (multimodal.py)
  TF3P_COVERAGE    "1"/"0"  phase 2: after max-profit projects connect the rest if not loss-making

The same variables are set for every job the control center runs (dossier, diagnostics),
so modules that build a Planner can honour them too.
"""
from __future__ import annotations

import json
import threading

from .paths import SETTINGS

DEFAULTS: dict = {
    "horizon": 2030,
    "passengers": True,
    "multimodal": True,
    "coverage": True,
    "auto_mode": True,          # watch export.lua and rebuild the plan automatically
    "auto_calibrate": True,     # run calibration in the auto pipeline (needs export v5 with lines)
    "auto_dossier": True,       # run the dossier in the auto pipeline (if tf3planner/dossier.py exists)
    "notify_desktop": True,     # browser notification in addition to the toast
    "lang": "ru",               # language of the plan texts in the in-game blueprint ("ru" / "en")
}

ENV_DOC = {
    "TF3P_HORIZON": "последний год расчёта плана",
    "TF3P_PASSENGERS": "1/0 — пассажирские линии",
    "TF3P_MULTIMODAL": "1/0 — цепочки через склады (перевалка)",
    "TF3P_COVERAGE": "1/0 — фаза 2: охват остального без убытка",
}

_lock = threading.Lock()


def load() -> dict:
    s = dict(DEFAULTS)
    try:
        raw = json.loads(SETTINGS.read_text(encoding="utf-8"))
        if isinstance(raw, dict):
            s.update({k: raw[k] for k in DEFAULTS if k in raw})
    except (OSError, ValueError):
        pass
    return s


def save(update: dict) -> dict:
    with _lock:
        s = load()
        for k, v in (update or {}).items():
            if k not in DEFAULTS:
                continue
            d = DEFAULTS[k]
            if isinstance(d, bool):
                v = bool(v)
            elif k == "lang":
                if v not in ("ru", "en"):
                    continue
            elif isinstance(d, int):
                try:
                    v = int(v)
                except (TypeError, ValueError):
                    continue
                if k == "horizon":
                    v = max(1850, min(2200, v))
            s[k] = v
        try:        # keep keys owned by others (path overrides "userdata_dir"/"game_dir": tf3planner/config.py)
            raw = json.loads(SETTINGS.read_text(encoding="utf-8"))
            extra = {k: v for k, v in raw.items() if k not in DEFAULTS} if isinstance(raw, dict) else {}
        except (OSError, ValueError):
            extra = {}
        SETTINGS.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS.write_text(json.dumps({**s, **extra}, ensure_ascii=False, indent=1), encoding="utf-8")
        return s


def planner_env(s: dict | None = None) -> dict[str, str]:
    s = s or load()
    return {
        "TF3P_HORIZON": str(int(s.get("horizon") or 0)),
        "TF3P_PASSENGERS": "1" if s.get("passengers") else "0",
        "TF3P_MULTIMODAL": "1" if s.get("multimodal") else "0",
        "TF3P_COVERAGE": "1" if s.get("coverage") else "0",
    }

"""Localized JSON answers for the web server and the control center (see tf3planner/i18n.py).

``JSONResponse`` translates its content into the language of the current request just
before it is rendered. Big files (plan.json, dossiers.json, routes.json) are translated once
per language and file version by ``localized_file`` and then served from memory.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any

from fastapi.responses import JSONResponse as _JSONResponse

from tf3planner import i18n

_lock = threading.Lock()
_files: dict[tuple[str, str], tuple[float, Any]] = {}


class JSONResponse(_JSONResponse):
    """JSON answer in the request's language; ``translated=True`` when the content already is."""

    def __init__(self, content: Any, *args, translated: bool = False, **kwargs):
        if not translated:
            content = i18n.localize(content)
        super().__init__(content, *args, **kwargs)


def localized_file(path: Path) -> Any:
    """Parsed JSON file translated into the current language (cached by file version)."""
    lang = i18n.get_lang()
    mtime = path.stat().st_mtime
    key = (str(path), lang)
    with _lock:
        hit = _files.get(key)
        if hit is not None and hit[0] == mtime:
            return hit[1]
    data = json.loads(path.read_text(encoding="utf-8"))
    data = i18n.localize(data, lang)
    with _lock:
        _files[key] = (mtime, data)
    return data


def warm_up(paths: list[Path], lang: str = "en") -> None:
    """Translate the big files in the background so the first page load is fast."""
    def run():
        token = i18n.set_lang(lang)
        for p in paths:
            try:
                if p.exists():
                    localized_file(p)
            except (OSError, ValueError):
                pass
        del token
    threading.Thread(target=run, name="i18n-warm-up", daemon=True).start()

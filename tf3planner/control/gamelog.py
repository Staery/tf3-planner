"""Tail of the game's stdout.txt filtered to the planner mod ([TF3P]) and Lua errors, plus hints.

The game rewrites crash_dump/stdout.txt on every start. Entries are a header line
"[2026-10-01 17:53:21Z - LEVEL - thread - cat]  message" followed by continuation lines
("    | ...", "    + ...").
"""
from __future__ import annotations

import re
from pathlib import Path

from .paths import GAME_STDOUT, MOD_NAME

TAIL_BYTES = 1_500_000
_HEAD = re.compile(r"^\[(\d{4}-\d\d-\d\d \d\d:\d\d:\d\dZ) - (\w+)\s*-\s*([^-\]]*?)\s*-\s*([^\]]*?)\s*\]\s?(.*)$")

HINTS = [
    {
        "id": "mod_file_missing",
        "re": re.compile(rf"module '{MOD_NAME}::.*not found|Could not find file {MOD_NAME}::", re.S),
        "level": "error",
        "title": "Игра не видит файл мода",
        "text": "Мод обновлён, а игра работает со старым списком файлов (он читается только при запуске). "
                "Полностью выйдите из игры (до рабочего стола Windows) и запустите её заново. Если ошибка "
                "останется — нажмите «Установить/обновить мод в игре» и перезапустите игру ещё раз.",
    },
    {
        "id": "export_crashed",
        "re": re.compile(r"\[TF3P\] export crashed"),
        "level": "error",
        "title": "Выгрузка упала внутри игры",
        "text": "Кнопка экспорта сработала, но скрипт мода завершился с ошибкой (текст — в журнале ниже). "
                "Попробуйте ещё раз после загрузки сейва; если повторяется — передайте текст ошибки разработчику.",
    },
    {
        "id": "section_failed",
        "re": re.compile(r"\[TF3P\] section \S+ failed"),
        "level": "warn",
        "title": "Часть данных не выгрузилась",
        "text": "Один из разделов выгрузки упал, остальные записаны. План посчитается, но без этого раздела. "
                "Список ошибок есть на карточке «Выгрузка из игры».",
    },
    {
        "id": "save_failed",
        "re": re.compile(r"\[TF3P\] saveUserdata failed"),
        "level": "error",
        "title": "Игра не смогла записать файл выгрузки",
        "text": "Проверьте, что диск D: не переполнен и папка local\\tf3_planner не открыта/не заблокирована.",
    },
    {
        "id": "ui_root_failed",
        "re": re.compile(r"Script component root failed"),
        "level": "error",
        "need": re.compile(MOD_NAME),
        "title": "Кнопка мода не загрузилась",
        "text": "Интерфейс мода упал при загрузке (подробности — в ошибке выше). Чаще всего лечится "
                "переустановкой мода и полным перезапуском игры.",
    },
    {
        "id": "layout",
        "re": re.compile(r"Recipe child must be a layout"),
        "level": "error",
        "title": "Ошибка разметки интерфейса мода",
        "text": "Ошибка в коде мода (корень плагина должен быть BoxLayout). Нужна правка мода разработчиком.",
    },
    {
        "id": "lua_runtime",
        "re": re.compile(r"attempt to (index|call|compare|perform arithmetic|concatenate)"),
        "level": "error",
        "need": re.compile(MOD_NAME + r"|\[TF3P\]"),
        "title": "Ошибка Lua в моде",
        "text": "Скрипт мода обратился к несуществующему полю (часто — после обновления игры). Передайте "
                "текст ошибки разработчику; остальная игра при этом работает.",
    },
    {
        "id": "int_format",
        "re": re.compile(r"number has no integer representation|bad argument #\d+ to 'format'"),
        "level": "error",
        "title": "Ошибка форматирования чисел",
        "text": "В Lua 5.2 игры %d падает на больших числах — нужна правка мода (использовать %.0f).",
    },
]

GENERIC = [
    {"id": "how_export", "level": "info", "title": "Как сделать выгрузку",
     "text": "В игре внизу экрана есть кнопка «TF3 Planner: Export». Нажмите её — надпись сменится на "
             "«exported», и через несколько секунд план обновится сам (если включён авторежим)."},
    {"id": "mod_not_enabled", "level": "info", "title": "Нет кнопки «TF3 Planner» в игре",
     "text": "Мод нужно включить для конкретного сейва: при загрузке сохранения откройте вкладку «Моды» "
             "в карточке сейва, включите «TF3 Planner» и загрузите. В новых играх — галочка при создании."},
    {"id": "restart", "level": "info", "title": "После обновления мода",
     "text": "Игра подхватывает новые файлы мода только после полного перезапуска (выход в Windows). "
             "Загрузить сейв заново недостаточно."},
]


def _entries(text: str) -> list[dict]:
    out: list[dict] = []
    cur = None
    for line in text.splitlines():
        m = _HEAD.match(line)
        if m:
            cur = {"ts": m.group(1), "level": m.group(2), "cat": m.group(4), "text": m.group(5).strip()}
            out.append(cur)
        elif not line.strip():
            continue
        elif cur is not None and line[:1] in (" ", "\t"):
            cur["text"] += "\n" + line.rstrip()
        else:   # plain print() output, e.g. "[TF3P] ..." - its own entry, time of the previous one
            cur = {"ts": cur["ts"] if cur else "", "level": "PRINT", "cat": "", "text": line.rstrip()}
            out.append(cur)
    return out


def read(limit: int = 200) -> dict:
    path = Path(GAME_STDOUT)
    if not path.exists():
        return {"exists": False, "path": str(path), "entries": [], "hints": GENERIC, "detected": []}
    size = path.stat().st_size
    with path.open("rb") as f:
        if size > TAIL_BYTES:
            f.seek(size - TAIL_BYTES)
        raw = f.read().decode("utf-8", "replace")
    if size > TAIL_BYTES:
        raw = raw.split("\n", 1)[-1]
    entries = _entries(raw)
    session_start = entries[0]["ts"] if entries and size <= TAIL_BYTES else None
    mod_seen = MOD_NAME in raw
    active_blocks = [e["text"] for e in entries if "Active mods:" in e["text"][:20]]
    mod_active = (MOD_NAME in active_blocks[-1]) if active_blocks else None   # in the loaded save
    keep = []
    for e in entries:
        t = e["text"]
        kind = None
        if "[TF3P]" in t:
            kind = "tf3p"
        elif e["level"] in ("ERROR", "CRITICAL") or "Lua error" in t or "Lua Error" in t:
            if "Texture error" in t:
                continue
            kind = "error"
        elif MOD_NAME in t and e["level"] != "VERBOSE" and "Active mods:" not in t[:20]:
            kind = "mod"
        if kind:
            keep.append({**e, "kind": kind, "mine": MOD_NAME in t or "[TF3P]" in t})
    # VERBOSE "Exception type: Lua error" blocks carry the details of the CRITICAL entry after them
    for e in entries:
        if e["level"] == "VERBOSE" and ("Lua error" in e["text"] or "Error message" in e["text"]):
            if not any(k is e or k.get("text") == e["text"] for k in keep):
                keep.append({**e, "kind": "error", "mine": MOD_NAME in e["text"]})
    keep.sort(key=lambda e: e["ts"])

    detected = []
    blob = "\n".join(e["text"] for e in keep)
    for h in HINTS:
        if h["re"].search(blob) and (h.get("need") is None or h["need"].search(blob)):
            detected.append({k: v for k, v in h.items() if k not in ("re", "need")})
    tf3p = [e for e in keep if e["kind"] == "tf3p"]
    if not mod_seen and size <= TAIL_BYTES:
        detected.append({"id": "mod_unknown", "level": "warn", "title": "Игра не загрузила мод",
                         "text": "В журнале текущего запуска игры нет упоминаний tf3_planner_1. Нажмите "
                                 "«Установить/обновить мод в игре» и перезапустите игру."})
    elif mod_active is False:
        detected.append({"id": "mod_not_active", "level": "warn", "title": "Мод не включён в загруженном сейве",
                         "text": "Игра видит мод, но в списке активных модов сейва его нет — кнопки экспорта не "
                                 "будет. Выйдите в главное меню, в карточке сейва откройте вкладку «Моды», включите "
                                 "«TF3 Planner» и загрузите сейв снова."})
    elif not tf3p:
        detected.append({"id": "no_export", "level": "info", "title": "В этом запуске игры выгрузки ещё не было",
                         "text": "Загрузите сейв и нажмите кнопку «TF3 Planner: Export» внизу экрана. Если кнопки "
                                 "нет — мод не включён в этом сейве (вкладка «Моды» в карточке сейва)."})
    return {"exists": True, "path": str(path), "size": size, "time": path.stat().st_mtime,
            "session_start": session_start, "mod_seen": mod_seen, "mod_active": mod_active,
            "entries": keep[-limit:], "hints": GENERIC, "detected": detected}

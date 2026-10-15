"""Install / update the in-game mod (Python port of deploy.sh).

  python -m tf3planner.control.deploy

Copies mod/tf3_planner_1 into <userdata>/mods/tf3_planner_1 and regenerates _content.json
from the actual files under content/ (the game only loads files listed there).
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

from .. import config
from .paths import GAME_EXE, MOD_DST, MOD_NAME, MOD_SRC, MODS_DIR, NO_WINDOW


def content_files(mod_dir: Path) -> list[str]:
    content = mod_dir / "content"
    if not content.is_dir():
        return []
    return sorted(p.relative_to(content).as_posix() for p in content.rglob("*") if p.is_file())


def write_hot_reload(mod_dir: Path) -> int:
    """<userdata>/tf3_planner/blueprint_hot.lua: the blueprint module's code twice — as a function in
    the data table and as a source string — plus blueprint_hot_ver.lua with its version; the mod's
    bar timer swaps the module when the version changes (GUI script changes still need a restart)."""
    import time
    src = (mod_dir / "content" / "tf3_planner" / "blueprint.lua").read_text(encoding="utf-8")
    eq = "=" * 3
    while f"]{eq}]" in src or f"[{eq}[" in src:
        eq += "="
    v = int(time.time())
    out = MODS_DIR.parent / "tf3_planner"
    out.mkdir(parents=True, exist_ok=True)
    nl = "\n"
    body = ("function data()" + nl + "return {" + nl + f"\tv = {v}," + nl + "\tfn = function()" + nl + src + nl
            + "end," + nl + f"\tsrc = [{eq}[" + nl + src + f"]{eq}]," + nl + "}" + nl + "end" + nl)
    tmp = out / "blueprint_hot.tmp"
    tmp.write_text(body, encoding="utf-8", newline=nl)
    tmp.replace(out / "blueprint_hot.lua")
    (out / "blueprint_hot_ver.lua").write_text("function data()" + nl + f"return {{ v = {v} }}" + nl + "end" + nl,
                                               encoding="utf-8")
    return v


def write_content_json(mod_dir: Path) -> int:
    files = content_files(mod_dir)
    body = ['{', '    "archives": null,', '    "files": [']
    body += [f'        {json.dumps(f, ensure_ascii=False)}' + ("," if i < len(files) - 1 else "")
             for i, f in enumerate(files)]
    body += ['    ]', '}']
    (mod_dir / "_content.json").write_text("\n".join(body) + "\n", encoding="utf-8")
    return len(files)


def _digest(mod_dir: Path) -> dict[str, str]:
    out = {}
    if not mod_dir.is_dir():
        return out
    for p in sorted(mod_dir.rglob("*")):
        if p.is_file() and p.name not in ("_content.json", "mod.io_fileid.txt", "texture_metadata_cache.lua"):
            out[p.relative_to(mod_dir).as_posix()] = hashlib.sha1(p.read_bytes()).hexdigest()
    return out


def export_version(mod_dir: Path) -> int | None:
    f = mod_dir / "content" / "tf3_planner" / "export.lua"
    try:
        m = re.search(r"EXPORT_VERSION\s*=\s*(\d+)", f.read_text(encoding="utf-8", errors="replace"))
        return int(m.group(1)) if m else None
    except OSError:
        return None


def game_running() -> bool:
    try:
        out = subprocess.run(["tasklist", "/FI", f"IMAGENAME eq {GAME_EXE}", "/NH"], capture_output=True,
                             timeout=5, creationflags=NO_WINDOW).stdout
        return GAME_EXE.lower() in out.decode("ascii", "ignore").lower()
    except Exception:
        return False


def mod_state() -> dict:
    src, dst = _digest(MOD_SRC), _digest(MOD_DST)
    changed = sorted(k for k in set(src) | set(dst) if src.get(k) != dst.get(k))
    dst_listed = []
    try:
        dst_listed = json.loads((MOD_DST / "_content.json").read_text(encoding="utf-8")).get("files") or []
    except (OSError, ValueError):
        pass
    unlisted = sorted(set(content_files(MOD_DST)) - set(dst_listed)) if dst else []
    deployed_mtime = None
    if (MOD_DST / "_content.json").exists():
        deployed_mtime = (MOD_DST / "_content.json").stat().st_mtime
    return {
        "deployed": bool(dst),
        "up_to_date": bool(dst) and not changed and not unlisted,
        "changed_files": changed[:30],
        "unlisted_files": unlisted[:30],
        "src_version": export_version(MOD_SRC),
        "deployed_version": export_version(MOD_DST) if dst else None,
        "deployed_time": deployed_mtime,
        "src": str(MOD_SRC),
        "dst": str(MOD_DST),
        "modio": [str(p) for p in config.modio_installed(MOD_NAME)],   # subscribed copy from the Mod Hub
    }


def deploy() -> int:
    if not MOD_SRC.is_dir():
        print(f"ОШИБКА: нет исходников мода {MOD_SRC}")
        return 2
    if not MODS_DIR.parent.is_dir():
        print(f"ОШИБКА: не найдена папка пользователя игры {MODS_DIR.parent}. "
              "Запустите игру хотя бы раз или укажите папку: переменная TF3P_USERDATA или "
              "`--set-userdata` (python -m tf3planner.config / tf3planner-cli.exe -m tf3planner.config).")
        return 2
    before = mod_state()
    try:                    # the built app's bundle may be read-only (Program Files): then only the copy
        write_content_json(MOD_SRC)
    except OSError:
        pass
    MODS_DIR.mkdir(parents=True, exist_ok=True)
    # the Mod Hub links an uploaded local mod to its mod.io page with this file: keep it
    link = MOD_DST / "_metadata" / "mod.io_fileid.txt"
    link_data = link.read_bytes() if link.is_file() else None
    if MOD_DST.exists():
        shutil.rmtree(MOD_DST)
    shutil.copytree(MOD_SRC, MOD_DST)
    if link_data is not None and not link.exists():
        link.write_bytes(link_data)
    n = write_content_json(MOD_DST)
    print(f"_content.json: {n} файлов")
    try:
        v = write_hot_reload(MOD_SRC)
        print(f"горячее обновление: чертёж v{v} (подхватится в игре без перезапуска)")
    except OSError as exc:
        print(f"горячее обновление не записано: {exc}")
    after = mod_state()
    print(f"мод скопирован -> {MOD_DST}")
    print(f"версия экспортёра: v{after['src_version']}"
          + (f" (было v{before['deployed_version']})" if before["deployed"] else " (установлен впервые)"))
    if before["changed_files"]:
        print("изменённые файлы: " + ", ".join(before["changed_files"][:15]))
    if not after["up_to_date"]:
        print("ВНИМАНИЕ: после копирования файлы отличаются: " + ", ".join(after["changed_files"]))
        return 1
    if after["modio"]:
        print("ВНИМАНИЕ: мод также установлен из Mod Hub (mod.io): " + ", ".join(after["modio"])
              + " — включайте в сейве только одну копию (или отпишитесь от mod.io-версии).")
    if game_running():
        print("ВНИМАНИЕ: игра запущена — полностью перезапустите игру (выход в Windows), "
              "иначе она не увидит новые файлы мода.")
    else:
        print("Готово. Запустите игру и включите мод «TF3 Planner» в карточке сейва (вкладка «Моды»).")
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(errors="replace")
    except AttributeError:
        pass
    sys.exit(deploy())

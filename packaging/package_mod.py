"""Prepare the in-game mod for publishing: validate it and make a clean copy + zip.

  .venv/Scripts/python -X utf8 packaging/package_mod.py [--install]

Output:
  dist/mod/tf3_planner_1/                 clean copy (what the Mod Hub upload should contain)
  dist/tf3_planner_1-v<rev>.<export>.zip  the same as a zip (manual upload / attaching to a release)
--install also copies the clean copy into <userdata>/mods (same as tf3planner.control.deploy), which
is where the in-game Mod Hub picks local mods for publishing.

Checks: modinfo.json parses and has name/description/tags, a gallery image _metadata/0.png|jpg,
_content.json lists exactly the files under content/, every .lua compiles (lupa), no stray files
(mod.io_fileid.txt of the author's upload, *.bak, logs).
"""
from __future__ import annotations

import json
import re
import shutil
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from tf3planner.control import deploy  # noqa: E402
from tf3planner.control.paths import MOD_NAME, MOD_SRC  # noqa: E402

DIST = ROOT / "dist"
STRAY = re.compile(r"(mod\.io_fileid\.txt|\.bak|\.log|\.tmp|~)$|(^|/)(__pycache__|\.git)(/|$)")


def clean_copy(dst: Path) -> list[str]:
    if dst.exists():
        shutil.rmtree(dst)
    skipped = []
    for p in sorted(MOD_SRC.rglob("*")):
        rel = p.relative_to(MOD_SRC).as_posix()
        if p.is_dir():
            continue
        if STRAY.search(rel):
            skipped.append(rel)
            continue
        q = dst / rel
        q.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(p, q)
    deploy.write_content_json(dst)
    return skipped


def validate(mod: Path) -> list[str]:
    problems = []
    try:
        info = json.loads((mod / "_metadata" / "modinfo.json").read_text(encoding="utf-8"))
        for k in ("name", "description", "tags", "authors"):
            if not info.get(k):
                problems.append(f"modinfo.json: пустое поле {k}")
    except (OSError, ValueError) as e:
        problems.append(f"modinfo.json: {e}")
    if not any((mod / "_metadata" / n).exists() for n in ("0.png", "0.jpg")):
        problems.append("нет картинки _metadata/0.png (packaging/make_preview.py)")
    try:
        mj = json.loads((mod / "mod.json").read_text(encoding="utf-8"))
        if mj.get("modId") != MOD_NAME:
            problems.append(f"mod.json: modId {mj.get('modId')} != {MOD_NAME}")
    except (OSError, ValueError) as e:
        problems.append(f"mod.json: {e}")
    listed = set(json.loads((mod / "_content.json").read_text(encoding="utf-8")).get("files") or [])
    actual = set(deploy.content_files(mod))
    if listed != actual:
        problems.append(f"_content.json не совпадает с content/: {sorted(listed ^ actual)[:10]}")
    try:
        from lupa.lua52 import LuaRuntime
        lua = LuaRuntime()
        load = lua.eval("function(src, name) local f, err = load(src, name); return err end")
        for f in sorted(mod.rglob("*.lua")):
            err = load(f.read_text(encoding="utf-8"), f.name)
            if err:
                problems.append(f"Lua: {err}")
    except ImportError:
        pass
    return problems


def main() -> int:
    install = "--install" in sys.argv[1:]
    out = DIST / "mod" / MOD_NAME
    skipped = clean_copy(out)
    problems = validate(out)
    mj = json.loads((out / "mod.json").read_text(encoding="utf-8"))
    ver = f"{mj.get('revision', 1)}.{deploy.export_version(out) or 0}"
    zpath = DIST / f"{MOD_NAME}-v{ver}.zip"
    with zipfile.ZipFile(zpath, "w", zipfile.ZIP_DEFLATED) as z:
        for p in sorted(out.rglob("*")):
            if p.is_file():
                z.write(p, (Path(MOD_NAME) / p.relative_to(out)).as_posix())
    n = sum(1 for p in out.rglob("*") if p.is_file())
    print(f"мод: {out} ({n} файлов), zip: {zpath} ({zpath.stat().st_size // 1024} KB), версия v{ver}")
    if skipped:
        print("пропущено: " + ", ".join(skipped))
    for pr in problems:
        print("ПРОБЛЕМА: " + pr)
    if install and not problems:
        dst = deploy.MOD_DST
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(out, dst)
        print(f"установлено -> {dst}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())

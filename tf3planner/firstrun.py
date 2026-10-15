"""First-run preparation of game-derived files (nothing from the game is shipped with the app).

  python -m tf3planner.firstrun            (built app: tf3planner-cli.exe -m tf3planner.firstrun)

* data/industry_recipes.json — production recipes, extracted from the player's own TF3 install
  (base/content/industries/*.zip) by tools/extract_recipes.py. Needed by the planner and the map.
* mod schema.lua — field names of engine types for the exporter. Shipped with the mod; regenerated
  from the install (tools/gen_schema.py) only in a source checkout where it is missing.

The packaged launcher calls `ensure()` on every start (cheap when the files exist).
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

from . import config


def _tool(name: str):
    """tools/<name>.py as a module (bundled as a top-level module in the built app)."""
    try:
        return __import__(name)
    except ImportError:
        tools = str(config.CODE_ROOT / "tools")
        if tools not in sys.path:
            sys.path.insert(0, tools)
        return __import__(name)


def recipes_path() -> Path:
    return config.data_dir() / "industry_recipes.json"


def ensure_recipes(force: bool = False, log=print) -> Path | None:
    out = recipes_path()
    if out.exists() and not force:
        return out
    game = config.game_dir()
    if game is None:
        log("industry_recipes.json: папка Transport Fever 3 не найдена — укажите её "
            "(TF3P_GAME или --set-game, см. python -m tf3planner.config)")
        return None
    t0 = time.time()
    try:
        n = _tool("extract_recipes").main(game, out)
    except Exception as e:  # noqa: BLE001 - report, the app must still start
        log(f"industry_recipes.json: ошибка извлечения из {game}: {type(e).__name__}: {e}")
        return None
    log(f"industry_recipes.json: {n} рецептов из {game} за {time.time() - t0:.1f} с")
    return out


def ensure_schema(log=print) -> Path | None:
    out = config.CODE_ROOT / "mod" / "tf3_planner_1" / "content" / "tf3_planner" / "schema.lua"
    if out.exists() or config.FROZEN:
        return out if out.exists() else None
    game = config.game_dir()
    if game is None:
        log("schema.lua: нет ни файла, ни папки игры для генерации")
        return None
    try:
        _tool("gen_schema").main(game, out)
    except Exception as e:  # noqa: BLE001
        log(f"schema.lua: ошибка генерации: {type(e).__name__}: {e}")
        return None
    return out


def ensure(log=print) -> dict:
    config.data_dir().mkdir(parents=True, exist_ok=True)
    return {"recipes": ensure_recipes(log=log), "schema": ensure_schema(log=log)}


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description="Prepare game-derived files (industry recipes, schema)")
    ap.add_argument("--force", action="store_true", help="re-extract the recipes (after a game update)")
    args = ap.parse_args(argv)
    config.data_dir().mkdir(parents=True, exist_ok=True)
    rec = ensure_recipes(force=args.force)
    sch = ensure_schema()
    print(f"recipes: {rec or 'НЕТ'}\nschema:  {sch or 'НЕТ'}")
    return 0 if rec else 1


if __name__ == "__main__":
    sys.exit(main())

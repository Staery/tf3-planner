"""Extract production recipes (stockListConfig) of all base industries into data/industry_recipes.json.

Each industries/<type>.zip contains <type>/<type>.con.lua with a literal
`local stockListConfig = { stocks = {...}, rules = {...}, industrialCapacity = N }`.
Rules: input = list of alternatives, each a list of amounts per INPUT stock (in stock order);
output = {cargo = amount}; capacity = rule duration/throughput (scaled by industry level in-game);
booster rules raise productivity when their inputs are supplied.
"""
from __future__ import annotations

import json
import re
import sys
import zipfile
from pathlib import Path

from lupa.lua52 import LuaRuntime, lua_type

if __package__ in (None, ""):            # run as a script: make `tf3planner` importable
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tf3planner import config  # noqa: E402

# game install: $TF3P_GAME, settings.json "game_dir" or Steam libraries (tf3planner/config.py)
GAME = config.game_dir()
OUT = config.data_dir() / "industry_recipes.json"


def table_literal(src: str, start: int) -> str:
    i = src.index("{", start)
    depth = 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i:j + 1]
    raise ValueError("unbalanced braces")


def to_py(o):
    if lua_type(o) != "table":
        return o
    keys = list(o.keys())
    if keys and all(isinstance(k, int) for k in keys) and sorted(keys) == list(range(1, len(keys) + 1)):
        return [to_py(o[k]) for k in range(1, len(keys) + 1)]
    return {str(k): to_py(v) for k, v in o.items()}


def cargo_name(res: str) -> str:
    # "::/cargos/logs/logs.cargo" -> "logs"
    return res.rsplit("/", 1)[-1].removesuffix(".cargo")


def main(game: Path | None = None, out_path: Path | None = None) -> int:
    """Read <game>/base/content/industries/*.zip, write `out_path`. Returns the number of recipes."""
    game = Path(game) if game else (GAME or config.require_game_dir())
    out_path = Path(out_path) if out_path else OUT
    ind_dir = game / "base" / "content" / "industries"
    if not ind_dir.is_dir():
        raise FileNotFoundError(f"нет папки {ind_dir} — это точно установка Transport Fever 3?")
    lua = LuaRuntime()
    recipes = {}
    for z in sorted(ind_dir.glob("*.zip")):
        with zipfile.ZipFile(z) as zf:
            for name in zf.namelist():
                if not name.endswith(".con.lua"):
                    continue
                src = zf.read(name).decode("utf-8")
                m = re.search(r"local\s+stockListConfig\s*=", src)
                if not m:
                    continue
                cfg = to_py(lua.eval(table_literal(src, m.end())))
                stocks = [{"type": s["type"], "cargo": cargo_name(s["cargoType"]), "capacity": s.get("capacity")}
                          for s in cfg.get("stocks", [])]
                inputs = [s["cargo"] for s in stocks if s["type"] == "INPUT_STOCK"]
                rules = []
                for r in cfg.get("rules", []):
                    out = r.get("output") or {}
                    rules.append({
                        "input": [dict(zip(inputs, alt)) if isinstance(alt, list) else alt for alt in (r.get("input") or [])],
                        "output": {cargo_name(k): v for k, v in (out.items() if isinstance(out, dict) else [])},
                        "capacity": r.get("capacity"),
                        "booster": bool(r.get("booster")),
                    })
                key = "::/industries/" + name.removesuffix(".lua")
                recipes[key] = {"stocks": stocks, "rules": rules,
                                "industrialCapacity": cfg.get("industrialCapacity")}
    if not recipes:
        raise RuntimeError(f"в {ind_dir} не найдено ни одного рецепта (формат игры изменился?)")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    tmp.write_text(json.dumps(recipes, ensure_ascii=False, indent=1), encoding="utf-8")
    tmp.replace(out_path)
    print(f"{len(recipes)} recipes -> {out_path}")
    return len(recipes)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Extract industry recipes from the TF3 install")
    ap.add_argument("--game", type=Path, default=None, help="Transport Fever 3 install folder (default: auto)")
    ap.add_argument("--out", type=Path, default=None, help=f"output file (default {OUT})")
    a = ap.parse_args()
    try:
        main(a.game, a.out)
    except (OSError, RuntimeError) as e:
        print(f"ОШИБКА: {e}", file=sys.stderr)
        sys.exit(2)

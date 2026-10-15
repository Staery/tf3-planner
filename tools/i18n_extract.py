"""Collect every Russian UI string of TF3 Planner (Python, JavaScript, Lua) for translation.

Python f-strings become templates with numbered placeholders, in the order of the
values: ``f"Линия {name}: {n} ед."`` -> ``"Линия {0}: {1} ед."``. The translator
(``tf3planner/i18n.py``) matches finished text against these templates, so text that
the planner has already written to plan.json can still be shown in English.

    python tools/i18n_extract.py            # list strings missing from the catalogues
    python tools/i18n_extract.py --stats    # counts per file

Used by ``tests/test_i18n.py``.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CYR = re.compile("[А-Яа-яЁё]")
# Russian constants that are regular expressions for parsing our own text, not UI text
_REGEX_HINT = re.compile(r"\\[sdwSDW.(\[]|\(\?[:P!=]|\[\^|\\\\|\(\.\+\?\)|\.\*\?|\(\[")
# letters drawn in the game world with the mod's own Cyrillic glyphs (not translatable text)
NOT_UI = {"С", "О", "П", "Д", "Т", "СК"}

PY_DIRS = ["tf3planner"]
PY_SKIP = {"tf3planner/i18n.py"}
JS_FILES = ["tf3planner/web/static/app.js", "tf3planner/web/static/control.js"]
HTML_FILES = ["tf3planner/web/static/index.html", "tf3planner/web/static/control.html"]
LUA_FILES = ["mod/tf3_planner_1/content/tf3_planner/blueprint.lua",
             "mod/tf3_planner_1/content/gui/tf3_planner/tf3_planner_button.script.lua"]


# --- Python --------------------------------------------------------------------------

def _docstring_ids(tree: ast.AST) -> set[int]:
    out = set()
    for n in ast.walk(tree):
        if (isinstance(n, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
                and n.body and isinstance(n.body[0], ast.Expr)
                and isinstance(n.body[0].value, ast.Constant)):
            out.add(id(n.body[0].value))
    return out


def fstring_template(node: ast.JoinedStr) -> str:
    parts, k = [], 0
    for v in node.values:
        if isinstance(v, ast.Constant):
            parts.append(str(v.value).replace("{", "{{").replace("}", "}}"))
        else:
            parts.append("{%d}" % k)
            k += 1
    return "".join(parts)


def python_strings() -> dict[str, str]:
    """Russian string -> first location. Templates use {0}, {1}, ...; literal braces are doubled."""
    found: dict[str, str] = {}
    for d in PY_DIRS:
        for path in sorted((ROOT / d).rglob("*.py")):
            rel = path.relative_to(ROOT).as_posix()
            if rel in PY_SKIP:
                continue
            tree = ast.parse(path.read_text(encoding="utf-8"))
            docs = _docstring_ids(tree)
            in_f: set[int] = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.JoinedStr):
                    in_f.update(id(v) for v in n.values)
            for n in ast.walk(tree):
                if isinstance(n, ast.JoinedStr):
                    key = fstring_template(n)
                elif (isinstance(n, ast.Constant) and isinstance(n.value, str)
                      and id(n) not in docs and id(n) not in in_f):
                    key = n.value
                else:
                    continue
                if not CYR.search(key) or _REGEX_HINT.search(key) or key in NOT_UI:
                    continue
                found.setdefault(key, f"{rel}:{n.lineno}")
    return found


# --- JavaScript / HTML / Lua ---------------------------------------------------------

def js_loc_keys(src: str) -> list[str]:
    """Keys of loc("...") calls (double- or single-quoted)."""
    keys = []
    for m in re.finditer(r"\bloc\((\"(?:[^\"\\]|\\.)*\"|'(?:[^'\\]|\\.)*')", src):
        lit = m.group(1)
        if lit[0] == "'":
            lit = '"' + lit[1:-1].replace('\\"', '"').replace('"', '\\"').replace("\\'", "'") + '"'
        keys.append(json.loads(lit))
    return keys


def html_texts(src: str) -> list[str]:
    from html.parser import HTMLParser
    out: list[str] = []

    class P(HTMLParser):
        def handle_starttag(self, tag, attrs):
            out.extend(v for k, v in attrs if k in ("title", "placeholder") and v and CYR.search(v))

        def handle_data(self, data):
            t = " ".join(data.split())
            if CYR.search(t):
                out.append(t)

    P().feed(src)
    return out


def lua_t_keys(src: str) -> list[str]:
    """Keys of T("...") calls in the mod (plain double-quoted Lua strings)."""
    keys = []
    for m in re.finditer(r'\bT\("((?:[^"\\]|\\.)*)"', src):
        keys.append(m.group(1).encode("utf-8").decode("unicode_escape").encode("latin-1").decode("utf-8")
                    if "\\" in m.group(1) else m.group(1))
    return keys


def load_json(rel: str) -> dict:
    p = ROOT / rel
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--stats", action="store_true")
    args = ap.parse_args()
    py = python_strings()
    cat = load_json("tf3planner/locales/en.json")
    missing = {k: w for k, w in py.items() if k not in cat}
    if args.stats:
        from collections import Counter
        c = Counter(w.rsplit(":", 1)[0] for w in py.values())
        for f, n in c.most_common():
            print(f"{n:5}  {f}")
    print(f"python: {len(py)} strings, {len(missing)} missing")
    for k, w in list(missing.items())[:40]:
        print(f"  {w}: {k!r}")
    return 1 if missing else 0


if __name__ == "__main__":
    sys.exit(main())
